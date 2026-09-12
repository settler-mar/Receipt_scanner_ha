"""GET/PUT /api/settings/mcp — MCP server config (ROADMAP Крок 4)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import models  # noqa: F401


@pytest.fixture()
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_get_defaults(client):
    body = client.get("/api/settings/mcp").json()
    assert body["config"]["enabled"] is True
    assert body["mount_path"] == "/mcp"
    write = next(t for t in body["tools"] if t["name"] == "add_to_shopping_list")
    assert write["kind"] == "write"
    assert write["access"] == "off"          # write tools closed by default
    assert write["available"] is False
    rd = next(t for t in body["tools"] if t["name"] == "search_products")
    assert rd["access"] == "read" and rd["available"] is True


def test_put_per_tool_levels_and_unknown_dropped(client):
    r = client.put("/api/settings/mcp", json={
        "enabled": True,
        "tools": {
            "add_to_shopping_list": "write",
            "add_receipt": "write",
            "list_receipts": "off",
            "unknown_tool": "write",
        },
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config"]["tools"]["add_to_shopping_list"] == "write"
    assert body["config"]["tools"]["list_receipts"] == "off"
    assert "unknown_tool" not in body["config"]["tools"]
    assert next(t for t in body["tools"] if t["name"] == "add_receipt")["available"] is True
    assert next(t for t in body["tools"] if t["name"] == "list_receipts")["available"] is False
    # persisted
    again = client.get("/api/settings/mcp").json()
    assert again["config"]["tools"]["add_receipt"] == "write"


def test_generic_key_route_rejects_mcp_config(client):
    assert client.get("/api/settings/mcp_config").status_code == 400
    assert client.put("/api/settings/mcp_config", json={"value": {}}).status_code == 400
