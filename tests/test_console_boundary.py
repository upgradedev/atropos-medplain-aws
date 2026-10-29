"""SPA navigation must not shadow API errors or the original judge journey."""

from fastapi.testclient import TestClient

from medplain.api import app


def test_packet_demo_remains_available_with_strict_browser_boundary():
    client = TestClient(app)
    response = client.get("/demo")
    assert response.status_code == 200
    assert "Atropos" in response.text
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "style-src-attr 'unsafe-inline'" in policy
    assert "https://*.tile.openstreetmap.org" in policy
    assert "geolocation=()" in response.headers["permissions-policy"]


def test_client_navigation_uses_built_console_or_visible_unavailable_reason(monkeypatch, tmp_path):
    import medplain.api

    monkeypatch.setattr(medplain.api, "CONSOLE", tmp_path)
    client = TestClient(app)
    missing = client.get("/console/maps")
    assert missing.status_code == 503
    assert "bundle is unavailable" in missing.json()["detail"]
    (tmp_path / "index.html").write_text("<title>Atropos operations test bundle</title>")
    for path in ["/", "/console", "/console/cases", "/console/maps", "/console/patient/public-capability"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "operations test bundle" in response.text
    assert client.get("/api/ops/not-an-endpoint").status_code == 404
    assert client.get("/api/not-an-endpoint").status_code == 404
