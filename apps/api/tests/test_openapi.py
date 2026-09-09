import pytest


@pytest.mark.django_db
def test_openapi_schema_served(client):
    resp = client.get("/api/schema")
    assert resp.status_code == 200
    assert "openapi" in resp.headers.get("content-type", "") or resp["content-type"].startswith(
        ("application/vnd.oai.openapi", "application/yaml")
    )
    body = resp.content.decode()
    assert "Akilent API" in body
    assert "/api/v1/messages" in body
    assert "ApiKeyAuth" in body
    # the {version} placeholder must be resolved
    assert "{version}" not in body


@pytest.mark.django_db
def test_swagger_ui_served(client):
    resp = client.get("/api/docs")
    assert resp.status_code == 200
    assert b"swagger" in resp.content.lower()
