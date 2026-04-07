"""
Railway GraphQL API client.

Reads/writes Railway service variables via the Railway v2 GraphQL API.
Requires the following env vars (Railway injects service/project/env IDs
automatically; the token must be created manually in Railway dashboard):

    RAILWAY_TOKEN          — Railway API token (create at railway.app/account/tokens)
    RAILWAY_SERVICE_ID     — injected automatically by Railway
    RAILWAY_PROJECT_ID     — injected automatically by Railway
    RAILWAY_ENVIRONMENT_ID — injected automatically by Railway
"""
from __future__ import annotations

import os

import httpx

_GRAPHQL_URL = "https://backboard.railway.app/graphql/v2"

_Q_VARIABLES = """
query Variables($serviceId: String!, $projectId: String!, $environmentId: String!) {
  variables(serviceId: $serviceId, projectId: $projectId, environmentId: $environmentId)
}
"""

_M_UPSERT = """
mutation VariableUpsert($input: VariableUpsertInput!) {
  variableUpsert(input: $input)
}
"""

_M_DELETE = """
mutation VariableDelete($input: VariableDeleteInput!) {
  variableDelete(input: $input)
}
"""


def _credentials() -> tuple[str, str, str, str] | None:
    """Return (token, service_id, project_id, environment_id) or None."""
    token = os.getenv("RAILWAY_TOKEN", "")
    service_id = os.getenv("RAILWAY_SERVICE_ID", "")
    project_id = os.getenv("RAILWAY_PROJECT_ID", "")
    env_id = os.getenv("RAILWAY_ENVIRONMENT_ID", "")
    if token and service_id and project_id and env_id:
        return token, service_id, project_id, env_id
    return None


def is_configured() -> bool:
    return _credentials() is not None


async def get_variables() -> dict[str, str]:
    """Return all service variables as {name: value}.  Raises on failure."""
    creds = _credentials()
    if not creds:
        raise RuntimeError(
            "Railway not configured. Set RAILWAY_TOKEN, RAILWAY_SERVICE_ID, "
            "RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID."
        )
    token, service_id, project_id, env_id = creds
    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.post(
            _GRAPHQL_URL,
            json={
                "query": _Q_VARIABLES,
                "variables": {
                    "serviceId": service_id,
                    "projectId": project_id,
                    "environmentId": env_id,
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Railway API error: {data['errors']}")
    return data["data"]["variables"] or {}


async def upsert_variable(name: str, value: str) -> bool:
    """Set a single variable.  Returns True on success."""
    creds = _credentials()
    if not creds:
        raise RuntimeError("Railway not configured.")
    token, service_id, project_id, env_id = creds
    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.post(
            _GRAPHQL_URL,
            json={
                "query": _M_UPSERT,
                "variables": {
                    "input": {
                        "serviceId": service_id,
                        "projectId": project_id,
                        "environmentId": env_id,
                        "name": name,
                        "value": value,
                    }
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Railway API error: {data['errors']}")
    return bool(data["data"].get("variableUpsert"))
