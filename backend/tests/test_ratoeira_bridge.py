"""Tests for the trusted session bridge (POST /api/ratoeira/session)."""
import pytest
from httpx import AsyncClient
from pydantic import SecretStr

from app.core.config import get_settings

SECRET = "bridge-test-secret"


@pytest.fixture
def bridge_on():
    settings = get_settings()
    old = settings.ratoeira_bridge_secret
    settings.ratoeira_bridge_secret = SecretStr(SECRET)
    yield
    settings.ratoeira_bridge_secret = old


def _post(client, role="admin", email="leo@example.com", secret=SECRET):
    headers = {"X-Ratoeira-Bridge": secret} if secret is not None else {}
    return client.post(
        "/api/ratoeira/session",
        json={"email": email, "name": "Leo", "role": role},
        headers=headers,
    )


@pytest.mark.asyncio
async def test_bridge_off_without_secret(client: AsyncClient, clean_db):
    response = await _post(client)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_bridge_rejects_wrong_secret(client: AsyncClient, clean_db, bridge_on):
    assert (await _post(client, secret="nope")).status_code == 401
    assert (await _post(client, secret=None)).status_code == 401


@pytest.mark.asyncio
async def test_bridge_creates_user_and_shared_workspace(client: AsyncClient, clean_db, bridge_on):
    response = await _post(client)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["workspace_role"] == "owner"

    me = await client.get("/api/users/me", headers={"Authorization": f"Bearer {data['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "leo@example.com"
    assert me.json()["is_superuser"] is True

    # Second person joins the same workspace with the mapped role.
    other = (await _post(client, role="leitura", email="ana@example.com")).json()
    assert other["workspace_id"] == data["workspace_id"]
    assert other["workspace_role"] == "viewer"


@pytest.mark.asyncio
async def test_bridge_reapplies_role(client: AsyncClient, clean_db, bridge_on):
    first = (await _post(client, role="admin")).json()
    again = (await _post(client, role="financeiro")).json()
    assert again["user_id"] == first["user_id"]
    assert again["workspace_role"] == "editor"
    me = await client.get("/api/users/me", headers={"Authorization": f"Bearer {again['access_token']}"})
    assert me.json()["is_superuser"] is False
