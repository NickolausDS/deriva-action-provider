from datetime import datetime, timedelta, timezone
import logging
import multiprocessing
import os
import shutil
import subprocess

from flask import Flask, jsonify, request
from globus_action_provider_tools.authentication import TokenChecker
from globus_action_provider_tools.validation import (
    request_validator,
    response_validator
)
from isodate import duration_isoformat, parse_duration, parse_datetime
import jsonschema
from openapi_core.wrappers.flask import FlaskOpenAPIResponse, FlaskOpenAPIRequest
import requests

from cfde_ap import CONFIG
from . import actions, error as err, utils


# Flask setup
app = Flask(__name__)
app.config.from_mapping(**CONFIG)
app.url_map.strict_slashes = False

# Logging setup
logger = multiprocessing.get_logger()
logger.setLevel(CONFIG["LOG_LEVEL"])
logger.propagate = False
logfile_formatter = logging.Formatter("[{asctime}] [{levelname}] {name}: {message}",
                                      style='{',
                                      datefmt="%Y-%m-%d %H:%M:%S")
logfile_handler = logging.FileHandler(CONFIG["API_LOG_FILE"], mode='a')
logfile_handler.setFormatter(logfile_formatter)

logger.addHandler(logfile_handler)

logger.info("\n\n==========CFDE Action Provider started==========\n")

# Globals specific to this instance
TBL = CONFIG["DEMO_DYNAMO_TABLE"]
ROOT = "/"  # Segregate different APs by root path?
TOKEN_CHECKER = TokenChecker(CONFIG["GLOBUS_CC_APP"], CONFIG["GLOBUS_SECRET"],
                             [CONFIG["GLOBUS_SCOPE"]], CONFIG["GLOBUS_AUD"])

# Clean up environment
utils.clean_environment()


#######################################
# Flask helpers
#######################################

@app.errorhandler(err.ApiError)
def handle_invalid_usage(error):
    response = jsonify(error.to_dict())
    response.status_code = error.status
    return response


@app.before_request
def before_request():
    # Service alive check can skip validation
    if request.path == "/ping":
        return {"success": True}
    wrapped_req = FlaskOpenAPIRequest(request)
    validation_result = request_validator.validate(wrapped_req)
    if validation_result.errors:
        raise err.InvalidRequest("; ".join([str(err) for err in validation_result.errors]))
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    auth_state = TOKEN_CHECKER.check_token(token)
    if not auth_state.identities:
        # Return auth errors for debugging - may change in prod for security
        raise err.NoAuthentication("; ".join([str(err) for err in auth_state.errors]))
    request.auth = auth_state


@app.after_request
def after_request(response):
    wrapped_req = FlaskOpenAPIRequest(request)
    wrapped_resp = FlaskOpenAPIResponse(response)
    validation_result = response_validator.validate(wrapped_req, wrapped_resp)
    if validation_result.errors:
        logger.error("Error on response: {}, {}"
                     .format(response.response, validation_result.errors))
        raise err.DeveloperError("; ".join([str(err) for err in validation_result.errors]))
    return response


#######################################
# API Routes
#######################################

@app.route(ROOT, methods=["GET"])
def meta():
    resp = {
        "types": ["Action"],
        "api_version": "1.0",
        "globus_auth_scope": CONFIG["GLOBUS_SCOPE"],
        "title": "CFDE Demo Deriva Ingest",
        "subtitle": ("A Globus Automate Action Provider to demonstrate ingestion "
                     "of a properly-formatted BDBag into DERIVA."),
        # "description": "",
        # "keywords": [],
        "visible_to": ["all_authenticated_users"],
        "runnable_by": ["urn:globus:groups:id:" + CONFIG["GLOBUS_GROUP"]],
        # "administered_by": [],
        # "admin_contact": "",
        "synchronous": False,
        "log_supported": False,
        # "maximum_deadline": "",
        "input_schema": CONFIG["INPUT_SCHEMA"],
        # "event_types": [],  # Event-type providers only
    }
    if not request.auth.check_authorization(resp["visible_to"],
                                            allow_all_authenticated_users=True):
        raise err.NotAuthorized("You cannot view this Action Provider.")
    return jsonify(resp)


@app.route(ROOT+"run", methods=["POST"])
def run():
    req = request.get_json(force=True)
    # Validate input
    body = req.get("body", {})
    try:
        jsonschema.validate(body, CONFIG["INPUT_SCHEMA"])
    except jsonschema.ValidationError as e:
        # Raise just the first line of the exception text, which contains the error
        # The entire body and schema are in the exception, which are too verbose
        raise err.InvalidRequest(str(e).split("\n")[0])
    # Must have data_url if ingest or restore
    if body["operation"] in ["ingest", "restore"] and not body.get("data_url"):
        raise err.InvalidRequest("You must provide a data_url to ingest or restore.")
    # Must have catalog_id to modify and content to change (currently only ACL)
    elif body["operation"] == "modify":
        if not body.get("catalog_id"):
            raise err.InvalidRequest("You must specify the catalog_id to modify.")
        elif not body.get("catalog_acls"):
            raise err.InvalidRequest("You must specify content in the catalog to modify. "
                                     "(Currently only catalog_acls qualifies.)")
    # If request_id has been submitted before, return status instead of starting new
    try:
        status = utils.read_action_by_request(TBL, req["request_id"])
    # Otherwise, create new action
    except err.NotFound:
        # TODO: Accurately estimate completion time
        estimated_completion = datetime.now(tz=timezone.utc) + timedelta(days=1)

        default_release_after = timedelta(days=30)
        job = {
            # Start job as ACTIVE - no "waiting" status
            "status": "ACTIVE",
            # Default these to the principals of whoever is running this action:
            "manage_by": request.auth.identities,
            "monitor_by": request.auth.identities,
            "creator_id": request.auth.effective_identity,
            "release_after": default_release_after,
            "request_id": req["request_id"]
        }
        if "label" in req:
            job["label"] = req["label"]
        # Allow overriding by the request:
        if "manage_by" in req:
            job["manage_by"] = req["manage_by"]
        if "monitor_by" in req:
            job["monitor_by"] = req["monitor_by"]
        if "release_after" in req:
            job["release_after"] = parse_duration(req["release_after"]).tdelta
        if "deadline" in req:
            deadline = parse_datetime(req["deadline"])
            if deadline < estimated_completion:
                raise err.InvalidRequest(
                    f"Processing likely to exceed deadline of {req['deadline']}"
                )
        # Correct types for JSON serialization and DynamoDB ingest
        if isinstance(job["manage_by"], str):
            job["manage_by"] = [job["manage_by"]]
        else:
            job["manage_by"] = list(job["manage_by"])
        if isinstance(job["monitor_by"], str):
            job["monitor_by"] = [job["monitor_by"]]
        else:
            job["monitor_by"] = list(job["monitor_by"])
        # Standardize datetime to ISO format
        job["release_after"] = duration_isoformat(job["release_after"])

        # Create status in database (creates action_id)
        job = utils.create_action_status(TBL, job)

        # start_action() blocks, throws exception on failure, returns on success
        start_action(job["action_id"], req["body"])

        res = jsonify(utils.translate_status(job))
        res.status_code = 202
        return res
    else:
        return jsonify(utils.translate_status(status))


@app.route(ROOT+"<action_id>/status", methods=["GET"])
def status(action_id):
    status = utils.read_action_status(TBL, action_id)
    if not request.auth.check_authorization(status["monitor_by"]):
        raise err.NotAuthorized("You cannot view the status of action {}".format(action_id))
    return jsonify(utils.translate_status(status))


@app.route(ROOT+"<action_id>/cancel", methods=["POST"])
def cancel(action_id):
    status = utils.read_action_status(TBL, action_id)
    if not request.auth.check_authorization(status["manage_by"]):
        raise err.NotAuthorized("You cannot cancel action {}".format(action_id))

    clean_status = utils.translate_status(status)
    if clean_status["status"] in ["SUCCEEDED", "FAILED"]:
        raise err.InvalidState("Action {} already completed".format(action_id))

    cancel_action(action_id)
    new_status = utils.read_action_status(TBL, action_id)
    return jsonify(utils.translate_status(new_status))


@app.route(ROOT+"<action_id>/release", methods=["POST"])
def release(action_id):
    status = utils.read_action_status(TBL, action_id)
    if not request.auth.check_authorization(status["manage_by"]):
        raise err.NotAuthorized("You cannot cancel action {}".format(action_id))

    clean_status = utils.translate_status(status)
    if clean_status["status"] in ["ACTIVE", "INACTIVE"]:
        raise err.InvalidState("Action {} not completed and cannot be released".format(action_id))

    utils.delete_action_status(TBL, action_id)
    return clean_status


#######################################
# Synchronous events
#######################################

def start_action(action_id, action_data):
    # Process keyword catalog ID
    if action_data.get("catalog_id") in CONFIG["KNOWN_CATALOGS"].keys():
        catalog_info = CONFIG["KNOWN_CATALOGS"][action_data["catalog_id"]]
        action_data["catalog_id"] = catalog_info["catalog_id"]
        # Server must either not be provided, or must match catalog_info exactly
        if not action_data.get("server"):
            action_data["server"] = catalog_info["server"]
        elif action_data["server"] != catalog_info["server"]:
            raise ValueError(f"Server '{action_data['server']}' does not match server for "
                             f"catalog '{action_data['catalog_id']}' ({catalog_info['server']})")

    # TODO: Process management
    #       Currently assuming process manages itself
    # Restore Action
    if action_data["operation"] == "restore":
        logger.info(f"{action_id}: Starting Deriva restore into "
                    f"{action_data.get('catalog_id', 'new catalog')}")
        # Spawn new process
        args = (action_id, action_data["data_url"], action_data.get("server"),
                action_data.get("catalog_id"))
        driver = multiprocessing.Process(target=action_restore, args=args, name=action_id)
        driver.start()
    # Ingest Action
    elif action_data["operation"] == "ingest":
        logger.info(f"{action_id}: Starting Deriva ingest into "
                    f"{action_data.get('catalog_id', 'new catalog')}")
        # Spawn new process
        args = (action_id, action_data["data_url"], action_data.get("server"),
                action_data.get("catalog_id"), action_data.get("catalog_acls"))
        driver = multiprocessing.Process(target=action_ingest, args=args, name=action_id)
        driver.start()
    elif action_data["operation"] == "modify":
        logger.info(f"{action_id}: Starting Deriva modification of "
                    f"{action_data['catalog_id']}")
        # Spawn new process
        args = (action_id, action_data["catalog_id"], action_data.get("server"),
                action_data.get("catalog_acls"))
        driver = multiprocessing.Process(target=action_modify, args=args, name=action_id)
        driver.start()
    else:
        raise err.InvalidRequest("Operation '{}' unknown".format(action_data["operation"]))
    return


def cancel_action(action_id):
    # This action doesn't implement cancellation,
    # which is valid according to the Automate spec.
    # This is a stub in case cancellation is implemented later.
    return


#######################################
# Asynchronous actions
#######################################

def action_restore(action_id, url, server=None, catalog=None):
    token = utils.get_deriva_token()
    if not server:
        server = CONFIG["DEFAULT_SERVER_NAME"]

    # Download backup zip file
    # TODO: Determine file type
    #       Use original file name (Content-Disposition)
    #       Make filename unique if collision

    # Excessive try-except blocks because there's (currently) no process management;
    # if the action fails, it needs to always self-report failure

    logger.debug(f"{action_id}: Deriva restore process started")
    # Setup
    try:
        file_path = os.path.join(CONFIG["DATA_DIR"], "cfde-backup.zip")
    except Exception as e:
        error_status = {
            "status": "FAILED",
            "details": {
                "error": "Error in action setup: " + str(e)
            }
        }
        logger.error(f"{action_id}: Error in action setup: {repr(e)}")
        # If update fails, last-ditch effort is write to error file for debugging
        try:
            utils.update_action_status(TBL, action_id, error_status)
        except Exception as e2:
            with open("ERROR.log", 'w') as out:
                out.write(f"Error updating status on {action_id}: '{repr(e2)}'\n\n"
                          f"After error '{repr(e)}'")
        return
    # TODO: Check that catalog exists - non-existent catalog will fail

    logger.debug(f"{action_id}: Downloading '{url}'")
    # Download link
    try:
        with open(file_path, 'wb') as output:
            output.write(requests.get(url).content)
    except Exception as e:
        error_status = {
            "status": "FAILED",
            "details": {
                "error": f"Unable to download URL '{url}': {str(e)}"
            }
        }
        try:
            utils.update_action_status(TBL, action_id, error_status)
        except Exception as e2:
            with open("ERROR.log", 'w') as out:
                out.write(f"Error updating status on {action_id}: '{repr(e2)}'\n\n"
                          f"After error '{repr(e)}'")
        return

    # TODO: Use package calls instead of subprocess
    logger.debug(f"{action_id}: Restoring with script")
    try:
        restore_args = [
            "deriva-restore-cli",
            "--oauth2-token",
            token
        ]
        if catalog is not None:
            restore_args.extend([
                "--catalog",
                catalog
            ])
        restore_args.extend([
            server,
            file_path
        ])
        restore_res = subprocess.run(restore_args, capture_output=True)
        restore_message = restore_res.stderr + restore_res.stdout
    except Exception as e:
        error_status = {
            "status": "FAILED",
            "details": {
                "error": f"Unable to run restore script: {str(e)}"
            }
        }
        logger.error(f"{action_id}: Unable to run restore script: {repr(e)}")
        try:
            utils.update_action_status(TBL, action_id, error_status)
        except Exception as e2:
            with open("ERROR.log", 'w') as out:
                out.write(f"Error updating status on {action_id}: '{repr(e2)}'\n\n"
                          f"After error '{repr(e)}'")
        return

    # TODO: Check success, fetch ID without needing to parse output text
    try:
        if b"completed successfully" not in restore_message:
            raise ValueError(f"DERIVA restore failed: {restore_message}")
        deriva_link = (restore_message.split(b"Restore of catalog")[-1]
                                      .split(b"completed successfully")[0].strip())
        deriva_id = int(deriva_link.split(b"/")[-1])
        deriva_samples = f"https://{server}/chaise/recordset/#{deriva_id}/demo:Samples"
    except Exception as e:
        error_status = {
            "status": "FAILED",
            "details": {
                "error": f"Restore script output parsing failed: {str(e)}"
            }
        }
        logger.error(f"{action_id}: Restore script output parsing failed: {repr(e)}")
        try:
            utils.update_action_status(TBL, action_id, error_status)
        except Exception as e2:
            with open("ERROR.log", 'w') as out:
                out.write(f"Error updating status on {action_id}: '{repr(e2)}'\n\n"
                          f"After error '{repr(e)}'")
        return

    # Successful restore
    logger.debug(f"{action_id}:Restore complete")
    status = {
        "status": "SUCCEEDED",
        "details": {
            "deriva_id": deriva_id,
            "deriva_samples_link": deriva_samples,
            "message": "DERIVA restore successful",
            "error": False
        }
    }
    try:
        utils.update_action_status(TBL, action_id, status)
    except Exception as e:
        with open("ERROR.log", 'w') as out:
            out.write(f"Error updating status on {action_id}: '{repr(e)}'\n\n"
                      f"After success on ID '{deriva_id}'")
    return


def action_ingest(action_id, url, servername=None, catalog_id=None, acls=None):
    # Download ingest BDBag
    # Excessive try-except blocks because there's (currently) no process management;
    # if the action fails, it needs to always self-report failure

    if not servername:
        servername = CONFIG["DEFAULT_SERVER_NAME"]

    logger.debug(f"{action_id}: Deriva ingest process started for {catalog_id or 'new catalog'}")
    # Setup
    try:
        if acls is None:
            acls = CONFIG["DEFAULT_ACLS"]
        data_dir = os.path.join(CONFIG["DATA_DIR"], action_id + "/")
    except Exception as e:
        error_status = {
            "status": "FAILED",
            "details": {
                "error": "Error in action setup: " + str(e)
            }
        }
        logger.error(f"{action_id}: Error in action setup: {repr(e)}")
        # If update fails, last-ditch effort is write to error file for debugging
        try:
            utils.update_action_status(TBL, action_id, error_status)
        except Exception as e2:
            with open("ERROR.log", 'w') as out:
                out.write(f"Error updating status on {action_id}: '{repr(e2)}'\n\n"
                          f"After error '{repr(e)}'")
        return

    # TODO: Check that catalog exists if catalog_id set
    # Download and unarchive link
    logger.debug(f"{action_id}: Downloading '{url}'")
    try:
        bag_path = utils.download_data(url, data_dir)
        bag_data_path = os.path.join(bag_path, "data")
    except Exception as e:
        error_status = {
            "status": "FAILED",
            "details": {
                "error": f"Unable to download URL '{url}': {str(e)}"
            }
        }
        try:
            utils.update_action_status(TBL, action_id, error_status)
        except Exception as e2:
            with open("ERROR.log", 'w') as out:
                out.write(f"Error updating status on {action_id}: '{repr(e2)}'\n\n"
                          f"After error '{repr(e)}'")
        return

    # Find datapackage JSON file
    schema_file = "File not found"
    logger.debug(f"{action_id}: Determining schema file path")
    try:
        # Get schema file (assume exactly one non-hidden JSON file inside bag)
        schema_file = [filename for filename in os.listdir(bag_data_path)
                       if filename.endswith(".json") and not filename.startswith(".")][0]
        schema_file_path = os.path.join(bag_data_path, schema_file)
    except Exception as e:
        error_status = {
            "status": "FAILED",
            "details": {
                "error": f"Could not process TableSchema file '{schema_file}': {str(e)}"
            }
        }
        try:
            utils.update_action_status(TBL, action_id, error_status)
        except Exception as e2:
            with open("ERROR.log", 'w') as out:
                out.write(f"Error updating status on {action_id}: '{repr(e2)}'\n\n"
                          f"After error '{repr(e)}'")
        return

    # Ingest into Deriva
    logger.debug(f"{action_id}: Ingesting into Deriva")
    try:
        # TODO: Determine schema name from data
        schema_name = CONFIG["DERIVA_SCHEMA_NAME"]

        ingest_res = actions.deriva_ingest(servername, schema_file_path,
                                           catalog_id=catalog_id, acls=acls)
        if not ingest_res["success"]:
            error_status = {
                "status": "FAILED",
                "details": {
                    "error": f"Unable to ingest to DERIVA: {ingest_res.get('error')}"
                }
            }
            utils.update_action_status(TBL, action_id, error_status)
            return
        catalog_id = ingest_res["catalog_id"]
    except Exception as e:
        error_status = {
            "status": "FAILED",
            "details": {
                "error": f"Error ingesting to DERIVA: {str(e)}"
            }
        }
        logger.error(f"{action_id}: Error ingesting to DERIVA: {repr(e)}")
        try:
            utils.update_action_status(TBL, action_id, error_status)
        except Exception as e2:
            with open("ERROR.log", 'w') as out:
                out.write(f"Error updating status on {action_id}: '{repr(e2)}'\n\n"
                          f"After error '{repr(e)}'")
        return

    # Successful ingest
    logger.debug(f"{action_id}: Catalog {catalog_id} populated")
    status = {
        "status": "SUCCEEDED",
        "details": {
            "deriva_id": catalog_id,
            # "number_ingested": insert_count,
            "deriva_link": (f"https://{servername}/chaise/recordset/"
                            f"#{catalog_id}/{schema_name}:project"),
            "message": "DERIVA ingest successful",
            "error": False
        }
    }
    try:
        utils.update_action_status(TBL, action_id, status)
    except Exception as e:
        with open("ERROR.log", 'w') as out:
            out.write(f"Error updating status on {action_id}: '{repr(e)}'\n\n"
                      f"After success on ID '{catalog_id}'")

    # Remove ingested files from disk
    # Failed ingests are not removed, which helps debugging
    try:
        shutil.rmtree(data_dir)
    except Exception as e:
        logger.info(f"Data dir '{data_dir}' not deleted after ingest: {repr(e)}")
    return


def action_modify(action_id, catalog_id, servername=None, acls=None):
    # Modify the parameters of an existing catalog
    # Excessive try-except blocks because there's (currently) no process management;
    # if the action fails, it needs to always self-report failure
    # Argument acls defaults to None to allow different parameters later on

    if not servername:
        servername = CONFIG["DEFAULT_SERVER_NAME"]

    logger.debug(f"{action_id}: Deriva modify process started for {catalog_id}")

    # Modify Deriva catalog
    try:
        # TODO: Determine schema name from catalog
        schema_name = CONFIG["DERIVA_SCHEMA_NAME"]

        modify_res = actions.deriva_modify(servername, catalog_id, acls=acls)
        if not modify_res["success"]:
            error_status = {
                "status": "FAILED",
                "details": {
                    "error": f"Unable to modify catalog {catalog_id}: {modify_res.get('error')}"
                }
            }
            utils.update_action_status(TBL, action_id, error_status)
            return
    except Exception as e:
        error_status = {
            "status": "FAILED",
            "details": {
                "error": f"Error modifying DERIVA catalog {catalog_id}: {str(e)}"
            }
        }
        logger.error(f"{action_id}: Error modifying catalog {catalog_id}: {repr(e)}")
        try:
            utils.update_action_status(TBL, action_id, error_status)
        except Exception as e2:
            with open("ERROR.log", 'w') as out:
                out.write(f"Error updating status on {action_id}: '{repr(e2)}'\n\n"
                          f"After error '{repr(e)}'")
        return

    # Successful ingest
    logger.debug(f"{action_id}: Catalog {catalog_id} updated")
    status = {
        "status": "SUCCEEDED",
        "details": {
            "deriva_id": catalog_id,
            "deriva_link": (f"https://{servername}/chaise/recordset/"
                            f"#{catalog_id}/{schema_name}:project"),
            "message": "DERIVA catalog modification successful",
            "error": False
        }
    }
    try:
        utils.update_action_status(TBL, action_id, status)
    except Exception as e:
        with open("ERROR.log", 'w') as out:
            out.write(f"Error updating status on {action_id}: '{repr(e)}'\n\n"
                      f"After success on ID '{catalog_id}'")
    return
