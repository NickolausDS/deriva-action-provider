from copy import deepcopy
import logging
import os
import shutil

import boto3
from boto3.dynamodb.conditions import Attr
import bson  # For IDs
from deriva.core import DerivaServer
from deriva.core.ermrest_model import builtin_types, Table, Column, Key, ForeignKey
import globus_sdk
import mdf_toolbox

from cfde_ap import CONFIG
from . import error as err


logger = logging.getLogger(__name__)

DMO_CLIENT = boto3.resource('dynamodb',
                            aws_access_key_id=CONFIG["AWS_KEY"],
                            aws_secret_access_key=CONFIG["AWS_SECRET"],
                            region_name="us-east-1")
# DMO_TABLE = "cfde-demo-status"
DMO_SCHEMA = {
    # "TableName": DMO_TABLE,
    "AttributeDefinitions": [{
        "AttributeName": "action_id",
        "AttributeType": "S"
    }],
    "KeySchema": [{
        "AttributeName": "action_id",
        "KeyType": "HASH"
    }],
    "ProvisionedThroughput": {
        "ReadCapacityUnits": 20,
        "WriteCapacityUnits": 20
    }
}


def clean_environment():
    # Delete data dir and remake
    try:
        shutil.rmtree(CONFIG["DATA_DIR"])
    except FileNotFoundError:
        pass
    os.makedirs(CONFIG["DATA_DIR"])
    # Clear old exceptional error log
    try:
        os.remove("ERROR.log")
    except FileNotFoundError:
        pass


def initialize_dmo_table(table_name, schema=DMO_SCHEMA, client=DMO_CLIENT):
    """Init a table in DynamoDB, by default the DMO_TABLE with DMO_SCHEMA.
    Currently not intended to be called in a script;
    table creation is only necessary once per table.

    Arguments:
        table_name (str): The name for the DynamoDB table.
        schema (dict): The schema for the DynamoDB table.
                Default DMO_SCHEMA.
        client (dynamodb.ServiceResource): An authenticated client for DynamoDB.
                Default DMO_CLIENT.

    Returns:
        dynamodb.Table: The created DynamoDB table.

    Raises exception on any failure.
    """
    # Table should not be active already
    try:
        get_dmo_table(table_name, client)
    except err.NotFound:
        pass
    else:
        raise err.InvalidState("Table already created")

    schema = deepcopy(DMO_SCHEMA)
    schema["TableName"] = table_name

    try:
        new_table = client.create_table(**schema)
        new_table.wait_until_exists()
    except client.meta.client.exceptions.ResourceInUseException:
        raise err.InvalidState("Table concurrently created")
    except Exception as e:
        raise err.ServiceError(str(e))

    # Check that table now exists
    try:
        table2 = get_dmo_table(table_name, client)
    except err.NotFound:
        raise err.InternalError("Unable to create table")

    return table2


def get_dmo_table(table_name, client=DMO_CLIENT):
    """Return a DynamoDB table, by default the DMO_TABLE.

    Arguments:
        table_name (str): The name of the DynamoDB table.
        client (dynamodb.ServiceResource): An authenticated client for DynamoDB.
                Default DMO_CLIENT.

    Returns:
        dynamodb.Table: The requested DynamoDB table.

    Raises exception on any failure.
    """
    try:
        table = client.Table(table_name)
        dmo_status = table.table_status
        if dmo_status != "ACTIVE":
            raise ValueError("Table not active")
    except (ValueError, client.meta.client.exceptions.ResourceNotFoundException):
        raise err.NotFound("Table does not exist or is not active")
    except Exception as e:
        raise err.ServiceError(str(e))
    else:
        return table


def generate_action_id(table_name):
    """Generate a valid action_id, unique to the given table.

    Arguments:
        table_name (str): The name of the table to check uniqueness against.

    Returns:
        str: The action_id.
    """
    # TODO: Different ID generation logic?
    action_id = str(bson.ObjectId())
    while True:
        try:
            read_action_status(table_name, action_id)
        except err.NotFound:
            break
        else:
            action_id = str(bson.ObjectId())
    return action_id


def create_action_status(table_name, action_status):
    """Create action entry in status database (DynamoDB).

    Arguments:
        table_name (str): The name of the DynamoDB table.
        action_status (dict): The initial status for the action.

    Returns:
        dict: The action status created (including action_id).

    Raises exception on any failure.
    """
    table = get_dmo_table(table_name)

    # TODO: Add default status information
    action_id = generate_action_id(table_name)
    action_status["action_id"] = action_id
    if not action_status.get("details"):
        action_status["details"] = {
            "message": "Action started"
        }

    # TODO: Validate entry
    status_errors = []
    if status_errors:
        raise err.InvalidRequest(*status_errors)

    # Push to Dynamo table
    try:
        table.put_item(Item=action_status, ConditionExpression=Attr("action_id").not_exists())
    except Exception as e:
        logger.error("Error creating status for '{}': {}".format(action_id, str(e)))
        raise err.ServiceError(str(e))

    logger.info("{}: Action status created".format(action_id))
    return action_status


def read_action_status(table_name, action_id):
    """Fetch an action entry from status database.

    Arguments:
        table_name (str): The name of the table to read from.
        action_id (dict): The ID for the action.

    Returns:
        dict: The requested action status.

    Raises exception on any failure.
    """
    table = get_dmo_table(table_name)

    # If not found, Dynamo will return empty, only raising error on service issue
    try:
        entry = table.get_item(Key={"action_id": action_id}, ConsistentRead=True).get("Item")
    except Exception as e:
        logger.error("Error reading status for '{}': {}".format(action_id, str(e)))
        raise err.ServiceError(str(e))

    if not entry:
        raise err.NotFound("Action ID {} not found in status database".format(action_id))
    return entry


def read_action_by_request(table_name, request_id):
    """Fetch an action entry given its request_id instead of action_id.
    This requires scanning the DynamoDB table.

    Arguments:
        table_name (str): The name of the table to read from.
        request_id (str): The requested request_id.

    Returns:
        dict: The requested action status.

    Raises exception on any failure.
    """
    table = get_dmo_table(table_name)

    scan_args = {
        "ConsistentRead": True,
        "FilterExpression": Attr("request_id").eq(request_id)
    }
    # Make scan call, paging through if too many entries are scanned
    result_entries = []
    while True:
        scan_res = table.scan(**scan_args)
        # Check for success
        if scan_res["ResponseMetadata"]["HTTPStatusCode"] >= 300:
            logger.error("Scan error: {}: {}"
                         .format(scan_res["ResponseMetadata"]["HTTPStatusCode"],
                                 scan_res["ResponseMetadata"]))
            raise err.ServiceError(scan_res["ResponseMetadata"])
        # Add results to list
        result_entries.extend(scan_res["Items"])
        # Check for completeness
        # If LastEvaluatedKey exists, need to page through more results
        if scan_res.get("LastEvaluatedKey", None) is not None:
            scan_args["ExclusiveStartKey"] = scan_res["LastEvaluatedKey"]
        # Otherwise, all results retrieved
        else:
            break

    # Should be exactly 0 or 1 result, 2+ should never happen
    if len(result_entries) <= 0:
        raise err.NotFound("Request ID '{}' not found in status database".format(request_id))
    elif len(result_entries) == 1:
        return result_entries[0]
    else:
        logger.error("Multiple entries found for request ID '{}'!".format(request_id))
        raise err.InternalError("Multiple entries found for request ID '{}'. "
                                "Please report this error.".format(request_id))


def update_action_status(table_name, action_id, updates, overwrite=False):
    """Update action entry in status database.

    Arguments:
        table_name (str): The name of the table to update.
        action_id (dict): The ID for the action.
        updates (dict): The updates to apply to the action status.
        overwrite (bool): When False, will merge the updates into the existing status,
                overwriting only existing values.
                When True, will delete the existing status entirely and replace it
                with the updates.
                Default False.

    Returns:
        dict: The updated action status.

    Raises exception on any failure.
    """
    # Verify old status exists and save it
    old_status = read_action_status(table_name, action_id)

    # Merge updates into old_status if not overwriting
    if not overwrite:
        # dict_merge(base, addition) returns base keys unchanged, addition keys added
        full_updates = mdf_toolbox.dict_merge(updates, old_status)
    else:
        full_updates = updates

    # TODO: Validate updates
    update_errors = []
    if update_errors:
        raise err.InvalidRequest(*update_errors)

    # Update in DB (.put_item() overwrites)
    table = get_dmo_table(table_name)
    try:
        table.put_item(Item=full_updates)
    except Exception as e:
        logger.error("Error updating status for '{}': {}".format(action_id, str(e)))
        raise err.ServiceError(str(e))

    logger.debug("{}: Action status updated: {}".format(action_id, updates))
    return full_updates


def delete_action_status(table_name, action_id):
    """Release an action entry from the database.

    Arguments:
        table_name (str): The name of the table to delete from.
        action_id (dict): The ID for the action.

    Raises exception on any failure.
    """
    # Check that entry exists currently
    # Throws exceptions if it doesn't exist
    read_action_status(table_name, action_id)

    table = get_dmo_table(table_name)

    # Delete entry
    try:
        table.delete_item(Key={"action_id": action_id})
    except Exception as e:
        logger.error("Error deleting status for '{}': {}".format(action_id, str(e)))
        err.ServiceError(str(e))

    # Verify deletion
    try:
        read_action_status(table_name, action_id)
    except err.NotFound:
        pass
    else:
        logger.error("{} error: Action status in database after deletion".format(action_id))
        raise err.InternalError("Action status was not deleted.")

    logger.info("{}: Action status deleted".format(action_id))
    return


def translate_status(raw_status):
    """Translate raw status into user-servable form.

    Arguments:
        raw_status (dict): The status from the database to translate.

    Returns:
        dict: The translated status.
    """
    # TODO
    # DynamoDB stores int as Decimal, which isn't JSON-friendly
    if raw_status.get("details", {}).get("deriva_id"):
        raw_status["details"]["deriva_id"] = int(raw_status["details"]["deriva_id"])
    return raw_status


def get_deriva_token():
    # TODO: When decision is made about user auth vs. conf client auth, implement.
    #       Currently using personal refresh token for scope.
    #       Refresh token will expire in six months(?)
    #       Date last generated: 8-30-2019

    return globus_sdk.RefreshTokenAuthorizer(
                        refresh_token=CONFIG["TEMP_REFRESH_TOKEN"],
                        auth_client=globus_sdk.NativeAppAuthClient(CONFIG["GLOBUS_NATIVE_APP"])
           ).access_token


def create_deriva_catalog(servername, ermrest_schema, acls):
    """Create catalog on server with given schema and set ACLs.

    Arguments:
        servername (str): The server hostname. Only HTTPS servers are supported.
        ermrest_schema (dict): The ERMrest schema the catalog shoudl have.
        acls (dict of lists of str): The ACLs to set on the catalog.

    Returns:
        int: The catalog ID.
    """
    # Format credentials in DerivaServer-expected format
    creds = {
        "bearer-token": get_deriva_token()
    }
    server = DerivaServer("https", servername, creds)
    catalog = server.create_ermrest_catalog()
    catalog.post("/schema", json=ermrest_schema).raise_for_status()
    model = catalog.getCatalogModel()
    model.acls.update(acls)

    # TODO: Other config? (Chaise display params, etc.)

    model.apply(catalog)

    return catalog.catalog_id


def convert_tableschema(tableschema, schema_name):
    """Convert a TableSchema into ERMRest for a DERIVA catalog."""
    resources = tableschema["resources"]
    return {
        "schemas": {
            schema_name: {
                "schema_name": schema_name,
                "tables": {
                    tdef["name"]: make_table(tdef, schema_name)
                    for tdef in resources
                }
            }
        }
    }


def make_table(tdef, schema_name, provide_system=True):
    tname = tdef["name"]
    keys = []
    keysets = set()
    pk = tdef.get("primaryKey")
    if isinstance(pk, str):
        pk = [pk]
    if isinstance(pk, list):
        keys.append(make_key(tname, pk, schema_name))
        keysets.add(frozenset(pk))
    return Table.define(
        tname,
        column_defs=[
            make_column(cdef)
            for cdef in tdef["fields"]
        ],
        key_defs=([make_key(tname, pk, schema_name)] if pk else []) + [
            make_key(tname, [cdef["name"]], schema_name)
            for cdef in tdef["fields"]
            if cdef.get("constraints", {}).get("unique", False)
            and frozenset([cdef["name"]]) not in keysets
        ],
        fkey_defs=[
            make_fkey(tname, fkdef, schema_name)
            for fkdef in tdef.get("foreignKeys", [])
        ],
        comment=tdef.get("description"),
        provide_system=provide_system
    )


def make_type(col_type):
    """Choose appropriate ERMrest column types..."""
    if col_type == "string":
        return builtin_types.text
    elif col_type == "datetime":
        return builtin_types.timestamptz
    elif col_type == "integer":
        return builtin_types.int8
    elif col_type == "number":
        return builtin_types.float8
    elif col_type == "list":
        # assume a list is a list of strings for now...
        return builtin_types["text[]"]
    else:
        raise ValueError("Mapping undefined for type '{}'".format(col_type))


def make_column(cdef):
    constraints = cdef.get("constraints", {})
    return Column.define(
        cdef["name"],
        make_type(cdef.get("type", "string")),
        nullok=(not constraints.get("required", False)),
        comment=cdef.get("description"),
    )


def make_key(tname, cols, schema_name):
    return Key.define(
        cols,
        constraint_names=[[schema_name, "{}_{}_key".format(tname, "_".join(cols))]],
    )


def make_fkey(tname, fkdef, schema_name):
    fkcols = fkdef["fields"]
    fkcols = [fkcols] if isinstance(fkcols, str) else fkcols
    reference = fkdef["reference"]
    pktable = reference["resource"]
    pktable = tname if pktable == "" else pktable
    pkcols = reference["fields"]
    pkcols = [pkcols] if isinstance(pkcols, str) else pkcols
    return ForeignKey.define(
        fkcols,
        schema_name,
        pktable,
        pkcols,
        constraint_names=[[schema_name, "{}_{}_fkey".format(tname, "_".join(fkcols))]],
    )
