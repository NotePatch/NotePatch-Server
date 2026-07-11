from notepatch.entrypoints.api import app


def test_openapi_only_exposes_versioned_business_routes(client):
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    paths = set(response.json()["paths"])
    assert "/health" in paths
    assert all(path == "/health" or path.startswith("/api/v1/") for path in paths)
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/workspaces" in paths
    assert "/api/v1/admin/me" in paths
    assert "/api/v1/webhooks/tusd" in paths


def test_old_unversioned_routes_are_not_registered(client):
    assert client.post("/auth/login", json={}).status_code == 404
    assert client.get("/workspaces").status_code == 404
    assert client.get("/admin/api/me").status_code == 404
    assert client.post("/webhooks/tusd", json={}).status_code == 404


def test_docs_and_health_locations(client):
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/docs").status_code == 200
    assert client.get("/docs").status_code == 404
