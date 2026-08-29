from uuid import uuid4

from fastapi.testclient import TestClient

import main

SAMPLE_EVENT_ID = str(uuid4())
V1_PATHS = ("/v1/events", f"/v1/events/{SAMPLE_EVENT_ID}", "/v1/sources")
PUBLIC_PATHS = ("/", "/health", "/docs", "/redoc", "/openapi.json")


def _client(monkeypatch, **env: str) -> TestClient:
    monkeypatch.setenv("COLLECTOR_ENABLED", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RAPIDAPI_PROXY_SECRET", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return TestClient(main.app)


def test_health_and_root_skip_proxy_secret(monkeypatch) -> None:
    client = _client(monkeypatch, RAPIDAPI_PROXY_SECRET="test-secret")
    assert client.get("/health").status_code == 200
    assert client.get("/").json()["openapi"] == "/openapi.json"


def test_events_require_proxy_secret_when_configured(monkeypatch) -> None:
    client = _client(monkeypatch, RAPIDAPI_PROXY_SECRET="test-secret")
    denied = client.get("/v1/events")
    assert denied.status_code == 403
    allowed = client.get("/v1/events", headers={"X-RapidAPI-Proxy-Secret": "test-secret"})
    assert allowed.status_code == 503


def test_unauthenticated_v1_is_403_when_api_key_set(monkeypatch) -> None:
    client = _client(monkeypatch, API_KEY="origin-key")
    for path in V1_PATHS:
        assert client.get(path).status_code == 403


def test_unauthenticated_v1_is_403_when_proxy_secret_set(monkeypatch) -> None:
    client = _client(monkeypatch, RAPIDAPI_PROXY_SECRET="test-secret")
    for path in V1_PATHS:
        assert client.get(path).status_code == 403


def test_x_api_key_allows_v1_when_api_key_set(monkeypatch) -> None:
    client = _client(monkeypatch, API_KEY="origin-key")
    allowed = client.get("/v1/events", headers={"X-Api-Key": "origin-key"})
    assert allowed.status_code == 503
    allowed_sources = client.get("/v1/sources", headers={"X-Api-Key": "origin-key"})
    assert allowed_sources.status_code == 503
    allowed_one = client.get(
        f"/v1/events/{SAMPLE_EVENT_ID}", headers={"X-Api-Key": "origin-key"}
    )
    assert allowed_one.status_code == 503


def test_either_header_accepted_when_both_env_vars_set(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        RAPIDAPI_PROXY_SECRET="test-secret",
        API_KEY="origin-key",
    )
    via_proxy = client.get("/v1/events", headers={"X-RapidAPI-Proxy-Secret": "test-secret"})
    via_key = client.get("/v1/events", headers={"X-Api-Key": "origin-key"})
    assert via_proxy.status_code == 503
    assert via_key.status_code == 503
    assert client.get("/v1/events").status_code == 403


def test_public_paths_remain_open_when_origin_auth_configured(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        RAPIDAPI_PROXY_SECRET="test-secret",
        API_KEY="origin-key",
    )
    for path in PUBLIC_PATHS:
        assert client.get(path).status_code == 200
