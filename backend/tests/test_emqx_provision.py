"""emqx_provision.py pure builders — the authn user + ACL rule contract.

The broker half (HTTP calls) is verified live against EMQX; these tests pin
the SHAPES the provisioner writes, so a rule that would open the demo (a
wildcard farm, a publish rule for the subscriber, a subscribe rule for the
simulator) can never ship with a green suite.

فارسی: تستِ سازنده‌های قانونِ دسترسی — شکلی که روی بروکر می‌نشیند قفل شود.
"""

import importlib.util
from pathlib import Path

import pytest

PROV_PATH = Path(__file__).resolve().parents[2] / "scripts" / "emqx_provision.py"
if not PROV_PATH.exists():
    pytest.skip("emqx_provision.py not packaged in CI image", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("emqx_provision", PROV_PATH)
prov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prov)

MANIFEST = {
    "org_id": "org-1",
    "farms": [
        {"id": 6, "name": "Baharestan"},
        {"id": 8, "name": "Nahavand"},
        {"id": 7, "name": "Morvarid"},
    ],
}


def test_manifest_farm_ids_are_sorted_and_only_farms_with_saloons():
    assert prov.manifest_farm_ids(MANIFEST) == [6, 7, 8]
    assert prov.manifest_farm_ids({"farms": []}) == []


def test_sim_username_embeds_the_farm():
    assert prov.sim_username(6) == "demo-sim-farm6"
    assert prov.sim_username(7, prefix="sim") == "sim-farm7"


def test_sim_rules_pin_the_farm_and_never_subscribe():
    """A farm's simulator user may publish ONLY that farm's telemetry and
    status topics — the org is a single-level wildcard, the farm segment is
    literal, and there is no subscribe action anywhere."""
    rules = prov.sim_rules(6)
    assert len(rules) == 2
    for rule in rules:
        assert rule["permission"] == "allow"
        assert rule["action"] == "publish"
        assert set(rule) == {"permission", "action", "topic"}
        assert rule["topic"].startswith("+/farms/6/gateways/")
    topics = {r["topic"] for r in rules}
    assert topics == {
        "+/farms/6/gateways/+/telemetry",
        "+/farms/6/gateways/+/status",
    }
    # no wildcard farm, no cross-farm topic, no subscribe
    assert all("+/farms/7/" not in t and "farms/+/" not in t for t in topics)


def test_subscriber_rules_are_subscribe_only():
    """The subscriber ingests ALL farms' telemetry + pack liveness, so it may
    only subscribe — never publish. The telemetry rule uses the EFFECTIVE
    filter (EMQX checks $share subscriptions with the group prefix stripped)."""
    rules = prov.subscriber_rules("demo-sub")
    assert len(rules) == 2
    assert all(r["permission"] == "allow" and r["action"] == "subscribe" for r in rules)
    assert set(r["topic"] for r in rules) == {
        "+/farms/+/gateways/+/telemetry",
        "+/farms/+/gateways/+/status",
    }


def test_desired_users_covers_every_farm_plus_the_subscriber():
    users = prov.desired_users([6, 7, 8])
    assert users == [
        ("demo-sim-farm6", "devpass"),
        ("demo-sim-farm7", "devpass"),
        ("demo-sim-farm8", "devpass"),
        ("demo-sub", "devpass"),
    ]


def test_rules_by_username_has_every_identity():
    rules = prov.rules_by_username([6, 7, 8])
    assert set(rules) == {"demo-sim-farm6", "demo-sim-farm7", "demo-sim-farm8", "demo-sub"}
    # each sim user's topics belong to its own farm only
    for farm in (6, 7, 8):
        topics = [r["topic"] for r in rules[f"demo-sim-farm{farm}"]]
        assert all(f"/farms/{farm}/" in t for t in topics)


def test_rule_items_have_the_required_fields():
    """The EMQX 5.5 built-in-DB rule_item schema requires permission, action,
    topic — the provisioner must never emit a partial rule."""
    for rule in prov.subscriber_rules() + prov.sim_rules(6):
        assert {"permission", "action", "topic"} <= set(rule)
