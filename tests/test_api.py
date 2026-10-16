import os

from fastapi.testclient import TestClient

from app.errors import IdempotencyConflictError, StorageUnavailableError

os.environ["SKIP_STARTUP_INIT"] = "1"

from app.main import app


client = TestClient(app)


def test_health_smoke() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


def test_request_id_is_preserved_when_safe() -> None:
    response = client.get("/health", headers={"X-Request-ID": "trace-123"})

    assert response.headers["x-request-id"] == "trace-123"


def test_triage_smoke(monkeypatch) -> None:
    monkeypatch.setattr("app.main.fetch_recent_incidents", lambda limit=50: [])

    def fake_create_incident_record(**kwargs):
        return {
            "incident_id": "INC-TEST1234",
            "predicted_severity": kwargs["predicted_severity"],
            "duplicate_of": kwargs["duplicate_of"],
            "runbook_suggestion": kwargs["runbook_suggestion"],
            "status": "triaged",
        }

    monkeypatch.setattr("app.main.create_incident_record", fake_create_incident_record)

    payload = {
        "title": "Payment API latency spike",
        "description": "Timeout and degraded response time on checkout",
        "service": "payments-api",
    }

    response = client.post("/incidents/triage", json=payload)
    assert response.status_code == 200

    body = response.json()
    assert body["incident_id"] == "INC-TEST1234"
    assert body["status"] == "triaged"
    assert body["predicted_severity"] in {"low", "medium", "high", "critical"}


def test_validation_error_has_stable_shape() -> None:
    response = client.post("/incidents/triage", json={"title": "   "})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.json()["request_id"]


def test_unknown_incident_has_stable_not_found_response(monkeypatch) -> None:
    monkeypatch.setattr("app.main.get_incident", lambda incident_id: None)

    response = client.get("/incidents/INC-12345678")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_idempotency_key_is_forwarded(monkeypatch) -> None:
    monkeypatch.setattr("app.main.fetch_recent_incidents", lambda limit=50: [])
    captured = {}

    def fake_create_incident_record(**kwargs):
        captured.update(kwargs)
        return {
            "incident_id": "INC-TEST1234",
            "predicted_severity": kwargs["predicted_severity"],
            "duplicate_of": kwargs["duplicate_of"],
            "runbook_suggestion": kwargs["runbook_suggestion"],
            "status": "triaged",
        }

    monkeypatch.setattr("app.main.create_incident_record", fake_create_incident_record)
    response = client.post(
        "/incidents/triage",
        headers={"Idempotency-Key": "request-123"},
        json={"title": "Service down", "description": "Production outage"},
    )

    assert response.status_code == 200
    assert captured["idempotency_key"] == "request-123"


def test_invalid_idempotency_key_is_rejected() -> None:
    response = client.post(
        "/incidents/triage",
        headers={"Idempotency-Key": "invalid key"},
        json={"title": "Service down", "description": "Production outage"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_idempotency_conflict_returns_409(monkeypatch) -> None:
    monkeypatch.setattr("app.main.fetch_recent_incidents", lambda limit=50: [])

    def conflicting_create(**kwargs):
        raise IdempotencyConflictError("conflicting request")

    monkeypatch.setattr("app.main.create_incident_record", conflicting_create)
    response = client.post(
        "/incidents/triage",
        headers={"Idempotency-Key": "request-123"},
        json={"title": "Service down", "description": "Production outage"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_storage_failure_returns_503(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.main.fetch_recent_incidents",
        lambda limit=50: (_ for _ in ()).throw(StorageUnavailableError("offline")),
    )
    response = client.post(
        "/incidents/triage",
        json={"title": "Service down", "description": "Production outage"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"


def test_unexpected_failure_returns_generic_500_with_request_id(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.main.fetch_recent_incidents",
        lambda limit=50: (_ for _ in ()).throw(RuntimeError("private error")),
    )
    response = client.post(
        "/incidents/triage",
        json={"title": "Service down", "description": "Production outage"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.json()["error"]["message"] == "An unexpected error occurred"
    assert response.json()["request_id"] == response.headers["x-request-id"]
