"""M8-04-H — the gateway's FULL MQTT allow-rule contract (C5).

Before M8 the approval flow gave a gateway ONLY telemetry/status publish; the
real firmware additionally needs the downlink subscriptions (commands T3,
clock/sync T5, ota T6, diagnostics/request T8) and the uplink responses
(commands/response T4, ota/response T7, diagnostics/response T9) — under
`no_match=deny` a missing rule is a DENIED message, so this set IS the
firmware's MQTT contract. Pure-builder tests (no broker needed).

فارسی: قراردادِ کاملِ مجوزهای MQTT گیت‌وی در تاییدِ نصب — بدون این‌ها فریم‌ویر
واقعی زیرِ no_match=deny نمی‌تواند کار کند.
"""

from apps.provisioning.api import gateway_acl_rule_items

ORG = "11111111-2222-3333-4444-555555555555"
FARM = 12
SLOT = "FARM-01-GW-A"


def test_builder_covers_all_nine_topics():
    items = gateway_acl_rule_items(ORG, FARM, SLOT)
    assert len(items) == 9
    base = f"{ORG}/farms/{FARM}/gateways/{SLOT}"
    assert {i["topic"] for i in items} == {
        f"{base}/telemetry", f"{base}/status",
        f"{base}/commands/response", f"{base}/ota/response",
        f"{base}/diagnostics/response",
        f"{base}/commands", f"{base}/clock/sync", f"{base}/ota",
        f"{base}/diagnostics/request",
    }


def test_actions_match_the_contract():
    items = gateway_acl_rule_items(ORG, FARM, SLOT)
    by_action = {action: {i["topic"] for i in items if i["action"] == action}
                 for action in ("publish", "subscribe")}
    base = f"{ORG}/farms/{FARM}/gateways/{SLOT}"
    assert by_action["publish"] == {
        f"{base}/telemetry", f"{base}/status", f"{base}/commands/response",
        f"{base}/ota/response", f"{base}/diagnostics/response",
    }
    assert by_action["subscribe"] == {
        f"{base}/commands", f"{base}/clock/sync", f"{base}/ota",
        f"{base}/diagnostics/request",
    }


def test_every_rule_is_allow():
    assert all(i["permission"] == "allow" for i in gateway_acl_rule_items(ORG, FARM, SLOT))