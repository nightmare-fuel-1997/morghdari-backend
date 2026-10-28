"""billing_stub — the v1.0 billing PLACEHOLDER.

WHAT THIS IS
------------
A monthly invoice computed from the metering that already exists
(`org_daily_usage`): messages ingested, bytes stored, saloons, gateways. It
prints the invoice and can write it to a file. There is no payment provider,
no invoice table, no tenant-facing endpoint — deliberately. v1.0 asks for a
placeholder that can produce a bill for the first paying tenant, and a real
billing surface without a payment integration would be dead code with a
maintenance cost.

PRICING IS A KNOB, NOT AN OPINION
---------------------------------
The three prices come from SystemConfiguration and default to 0:

    billing_base_monthly      per-org monthly base fee
    billing_price_per_saloon  per active saloon
    billing_price_per_gateway per gateway

With the defaults every invoice is zero — that is the honest placeholder:
the METERING is real, the PRICING is the owner's decision to make. Set the
keys and the same command produces a priced invoice.

Usage:
    python manage.py billing_stub --month 2026-10
    python manage.py billing_stub --month 2026-10 --org <uuid> --json
    python manage.py billing_stub --month 2026-10 --out /backups/invoice.json
"""
import json
from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from apps.monitoring.models import OrgDailyUsage
from apps.system_config.models import SystemConfiguration
from apps.tenants.models import Organization

PRICE_KEYS = {
    "base_monthly": "billing_base_monthly",
    "per_saloon": "billing_price_per_saloon",
    "per_gateway": "billing_price_per_gateway",
}


def _price(key: str) -> Decimal:
    raw = (
        SystemConfiguration.objects.filter(key=key)
        .values_list("value", flat=True)
        .first()
    )
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal("0")


def _month_bounds(month: str) -> tuple[date, date]:
    try:
        year, mon = (int(x) for x in month.split("-"))
        first = date(year, mon, 1)
    except Exception as exc:  # noqa: BLE001
        raise CommandError("--month must be YYYY-MM") from exc
    last = date(year, mon, monthrange(year, mon)[1])
    return first, last


class Command(BaseCommand):
    help = "Produce a monthly invoice from org_daily_usage (v1.0 placeholder)."

    def add_arguments(self, parser):
        parser.add_argument("--month", required=True, help="YYYY-MM")
        parser.add_argument("--org", help="restrict to one org uuid")
        parser.add_argument("--json", action="store_true", help="emit JSON only")
        parser.add_argument("--out", help="also write the JSON to this path")

    def handle(self, *args, **opts):
        first, last = _month_bounds(opts["month"])
        qs = OrgDailyUsage.objects.filter(day__gte=first, day__lte=last)
        if opts.get("org"):
            qs = qs.filter(org_id=opts["org"])

        # Aggregate per org in one pass.
        per_org: dict[str, dict] = {}
        for row in qs.values(
            "org_id", "msg_count", "stored_bytes", "saloon_count", "gateway_count"
        ):
            agg = per_org.setdefault(
                str(row["org_id"]),
                {"messages": 0, "bytes": 0, "saloon_days": 0, "gateway_days": 0},
            )
            agg["messages"] += int(row["msg_count"] or 0)
            agg["bytes"] += int(row["stored_bytes"] or 0)
            agg["saloon_days"] += int(row["saloon_count"] or 0)
            agg["gateway_days"] += int(row["gateway_count"] or 0)

        base = _price(PRICE_KEYS["base_monthly"])
        per_saloon = _price(PRICE_KEYS["per_saloon"])
        per_gateway = _price(PRICE_KEYS["per_gateway"])
        days_in_month = (last - first).days + 1

        invoices = []
        for org_id, agg in sorted(per_org.items()):
            # A saloon counted on N days bills as N/days_of_month of a month —
            # so a saloon added on the 20th is not billed for the whole month.
            saloons = (Decimal(agg["saloon_days"]) / days_in_month).quantize(Decimal("0.01"))
            gateways = (Decimal(agg["gateway_days"]) / days_in_month).quantize(Decimal("0.01"))
            amount = (base + per_saloon * saloons + per_gateway * gateways).quantize(Decimal("0.01"))
            try:
                name = Organization.objects.using("admin").get(pk=org_id).name
            except Exception:  # noqa: BLE001 — name is cosmetic
                name = "(unknown org)"
            invoices.append({
                "org_id": org_id,
                "org_name": name,
                "month": opts["month"],
                "messages": agg["messages"],
                "stored_mb": round(agg["bytes"] / 1_048_576, 2),
                "saloon_months": float(saloons),
                "gateway_months": float(gateways),
                "base_fee": float(base),
                "saloon_fee": float((per_saloon * saloons).quantize(Decimal("0.01"))),
                "gateway_fee": float((per_gateway * gateways).quantize(Decimal("0.01"))),
                "total": float(amount),
                "pricing_configured": any((base, per_saloon, per_gateway)),
            })

        payload = {"month": opts["month"], "invoices": invoices}
        if opts.get("out"):
            with open(opts["out"], "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        if opts.get("json"):
            self.stdout.write(json.dumps(payload, indent=2))
            return
        if not invoices:
            self.stdout.write("no metered usage in %s" % opts["month"])
            return
        self.stdout.write(
            "%-24s %12s %10s %8s %8s %12s" %
            ("org", "messages", "stored_MB", "saloons", "gateways", "total")
        )
        for inv in invoices:
            self.stdout.write(
                "%-24s %12d %10.2f %8.2f %8.2f %12.2f" % (
                    inv["org_name"][:24], inv["messages"], inv["stored_mb"],
                    inv["saloon_months"], inv["gateway_months"], inv["total"],
                )
            )
        if not any(i["pricing_configured"] for i in invoices):
            self.stdout.write(
                "\nNOTE: pricing keys unset — totals are 0.00 by design "
                "(billing_base_monthly / billing_price_per_saloon / "
                "billing_price_per_gateway)."
            )
