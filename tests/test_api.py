"""Anonymous public path and fail-closed API boundary."""

import pytest
from fastapi.testclient import TestClient

from medplain.api import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MEDPLAIN_MODEL_MODE", "fixture")
    return TestClient(app)


def test_health_and_anonymous_source_selector(client):
    assert client.get("/").status_code == 200
    response = client.get("/api/health")
    assert response.json()["model_mode"] == "fixture"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    packets = client.get("/api/packets").json()["packets"]
    assert len(packets) == 3
    for packet in packets:
        assert client.get("/api/packets/" + packet["packet_id"]).json()["fhir"]["resourceType"] == "Bundle"


def test_actual_anonymous_stress_loop_and_receipt_verification(client):
    response = client.post("/api/explain", json={"packet_id": "dosage-mutation-stress"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "verified"
    assert result["correction_count"] >= 1
    assert result["explanation"]["medications"][0]["dose"] == "2.5"
    assert [r["verdict"] for r in result["receipts"]] == ["refused", "verified"]
    verify = client.post("/api/receipts/verify", json={"receipts": result["receipts"]})
    assert verify.json()["valid"]
    result["receipts"][0]["artifact_hash"] = "tampered"
    assert not client.post("/api/receipts/verify", json={"receipts": result["receipts"]}).json()["valid"]


def test_unknown_packet_and_private_uploads_rejected(client):
    assert client.get("/api/packets/unknown").status_code == 404
    assert client.post("/api/explain", json={"packet_id": "unknown"}).status_code == 404
    assert client.post("/api/explain", json={"packet_id": "home-after-pneumonia", "source_text": "PHI"}).status_code == 422
    assert client.post("/api/explain", content="plain data").status_code == 415
    assert client.post("/api/explain", content="x" * 131073,
                       headers={"content-type": "application/json"}).status_code == 413


def test_model_outage_never_publishes_unchecked_text(client, monkeypatch):
    import medplain.engine

    def broken(*args, **kwargs):
        raise RuntimeError("secret provider output")

    monkeypatch.setattr(medplain.engine, "run_packet", broken)
    response = client.post("/api/explain", json={"packet_id": "home-after-pneumonia"})
    assert response.status_code == 503
    assert "secret" not in response.text
    assert "No patient explanation was published" in response.text
