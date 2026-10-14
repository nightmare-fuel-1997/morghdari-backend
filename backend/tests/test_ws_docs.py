"""WS docs contract — the page and JSON schema cannot drift from ws.py builders."""

from apps.telemetry import ws


def test_ws_docs_page_serves_protocol(client):
    resp = client.get("/api/ws-docs/")
    assert resp.status_code == 200
    html = resp.content.decode()
    for needle in (
        "/ws/saloons/{saloon_id}/telemetry",
        "/ws/farms/{farmSlug}/telemetry",
        "4003",
        "4000",
        "telemetry.update",
        "telemetry.gap_fill",
        "connection_established",
        "/api/ws-schema/",
    ):
        assert needle in html, needle


def test_ws_schema_examples_match_builders(client):
    resp = client.get("/api/ws-schema/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["human_docs"] == "/api/ws-docs/"
    assert any(e["name"] == "saloon" for e in data["endpoints"])
    assert data["close_codes"]["4003"]
    assert data["close_codes"]["4000"]

    expected_update = ws.telemetry_update_message(
        saloon_id=12,
        slot_id="GW-01",
        device_ts="2026-09-12T10:00:00Z",
        metrics={"temp": {"avg": 24.5}, "humidity": {"avg": 62.0}},
        is_buffered=False,
    )
    assert data["server_messages"]["telemetry.update"]["example"] == expected_update

    expected_gap = ws.gap_fill_message(
        saloon_id=12,
        rows=[
            {
                "slot_id": "GW-01",
                "device_ts": "2026-09-12T10:00:00Z",
                "server_received_ts": "2026-09-12T10:00:02Z",
                "is_buffered": False,
                "metrics": {"temp": {"avg": 24.5}},
            }
        ],
    )
    assert data["server_messages"]["telemetry.gap_fill"]["example"] == expected_gap
