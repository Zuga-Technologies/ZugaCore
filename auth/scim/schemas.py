"""SCIM 2.0 wire shapes (RFC 7643 resources, RFC 7644 messages).

Only what this service provider implements is described: the User and
Group core schemas with the attributes we store, ListResponse, PatchOp,
the Error shape, ServiceProviderConfig and ResourceTypes. Every response
is `application/scim+json`.
"""

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

SCHEMA_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCHEMA_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCHEMA_LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCHEMA_PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCHEMA_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
SCHEMA_SPC = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
SCHEMA_RESOURCE_TYPE = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"

CONTENT_TYPE = "application/scim+json"

MAX_PAGE = 200
DEFAULT_PAGE = 100


class ScimError(HTTPException):
    """An RFC 7644 error: status, human detail and the optional scimType keyword."""

    def __init__(self, status_code: int, detail: str, scim_type: str | None = None, headers: dict | None = None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.scim_type = scim_type


def error_body(status: int, detail: str, scim_type: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"schemas": [SCHEMA_ERROR], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return body


def scim_json(content: Any, status_code: int = 200, location: str | None = None) -> JSONResponse:
    headers = {"Location": location} if location else None
    return JSONResponse(content=content, status_code=status_code, media_type=CONTENT_TYPE, headers=headers)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.isoformat(timespec="seconds") + "Z"
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _meta(resource_type: str, location: str, created: datetime | None, modified: datetime | None) -> dict[str, Any]:
    return {
        "resourceType": resource_type,
        "created": _iso(created),
        "lastModified": _iso(modified or created),
        "location": location,
    }


def user_resource(
    *,
    user_id: str,
    user_name: str,
    email: str,
    display_name: str | None,
    name: dict[str, Any] | None,
    external_id: str | None,
    active: bool,
    groups: list[tuple[str, str]],
    created: datetime | None,
    modified: datetime | None,
    base_url: str,
) -> dict[str, Any]:
    location = f"{base_url}/Users/{user_id}"
    resource: dict[str, Any] = {
        "schemas": [SCHEMA_USER],
        "id": user_id,
        "userName": user_name,
        "active": active,
        "emails": [{"value": email, "type": "work", "primary": True}],
        "groups": [{"value": gid, "display": gname, "$ref": f"{base_url}/Groups/{gid}"} for gid, gname in groups],
        "meta": _meta("User", location, created, modified),
    }
    if external_id is not None:
        resource["externalId"] = external_id
    if display_name:
        resource["displayName"] = display_name
    name_block = {k: v for k, v in (name or {}).items() if v}
    if display_name and not name_block.get("formatted"):
        name_block["formatted"] = display_name
    if name_block:
        resource["name"] = name_block
    return resource


def group_resource(
    *,
    group_id: str,
    display_name: str,
    external_id: str | None,
    members: list[tuple[str, str]],
    created: datetime | None,
    modified: datetime | None,
    base_url: str,
) -> dict[str, Any]:
    location = f"{base_url}/Groups/{group_id}"
    resource: dict[str, Any] = {
        "schemas": [SCHEMA_GROUP],
        "id": group_id,
        "displayName": display_name,
        "members": [{"value": uid, "display": email, "$ref": f"{base_url}/Users/{uid}"} for uid, email in members],
        "meta": _meta("Group", location, created, modified),
    }
    if external_id is not None:
        resource["externalId"] = external_id
    return resource


def list_response(resources: list[dict[str, Any]], total: int, start_index: int, count: int) -> dict[str, Any]:
    return {
        "schemas": [SCHEMA_LIST],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources) if count else 0,
        "Resources": resources,
    }


def service_provider_config(base_url: str) -> dict[str, Any]:
    return {
        "schemas": [SCHEMA_SPC],
        "documentationUri": "https://datatracker.ietf.org/doc/html/rfc7644",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": MAX_PAGE},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "OAuth Bearer Token",
                "description": "Authorization: Bearer <the organisation's SCIM token>",
                "primary": True,
            }
        ],
        "meta": {"resourceType": "ServiceProviderConfig", "location": f"{base_url}/ServiceProviderConfig"},
    }


def resource_types(base_url: str) -> dict[str, Any]:
    types = [
        {
            "schemas": [SCHEMA_RESOURCE_TYPE],
            "id": "User", "name": "User", "endpoint": "/Users", "schema": SCHEMA_USER,
            "meta": {"resourceType": "ResourceType", "location": f"{base_url}/ResourceTypes/User"},
        },
        {
            "schemas": [SCHEMA_RESOURCE_TYPE],
            "id": "Group", "name": "Group", "endpoint": "/Groups", "schema": SCHEMA_GROUP,
            "meta": {"resourceType": "ResourceType", "location": f"{base_url}/ResourceTypes/Group"},
        },
    ]
    return list_response(types, len(types), 1, len(types))
