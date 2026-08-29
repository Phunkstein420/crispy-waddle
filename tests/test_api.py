from fastapi.testclient import TestClient

import main


def test_health_and_root_skip_proxy_secret(monkeypatch) -> None:
    monkeypatch.setenv("RAPIDAPI_PROXY_SECRET", "test-secret")
    monkeypatch.setenv("COLLECTOR_ENABLED", "false")
    client = TestClient(main.app)
    assert client.get("/health").status_code == 200
    assert client.get("/").json()["openapi"] == "/openapi.json"


def test_events_require_proxy_secret_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("RAPIDAPI_PROXY_SECRET", "test-secret")
    monkeypatch.setenv("COLLECTOR_ENABLED", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(main.app)
    denied = client.get("/v1/events")
    assert denied.status_code == 403
    allowed = client.get("/v1/events", headers={"X-RapidAPI-Proxy-Secret": "test-secret"})
    assert allowed.status_code == 503
