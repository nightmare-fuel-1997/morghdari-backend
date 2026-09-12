"""legacy_slug_map writer — the ONE slug convention (compat spec §2).

Why this module exists (2026-09-12, contract v1.7.2): until now `legacy_slug_map`
had exactly two writers — the ops backfill command and admin-only creation — and
the rule "compat READS never write" held. The follow-through topology writes
(F1/F2) add the third writer and make it NORMATIVE: a farm or saloon created
through `POST /farms/` must resolve through the Flutter W-routes in the very
same request. Minting the row "later, when someone re-runs the backfill" fails
SILENTLY — the slug resolver answers 404, which is also its foreign/unknown
verdict, so the miss is indistinguishable from a typo'd slug.

The format is the backfill command's documented one, extracted here so the
command and the write path can never drift:

    org-<first 8 chars of the org uuid>   kind=org
    farm-<farm id>                        kind=farm
    device-<saloon id>                    kind=device   (Flutter "device" == backend saloon)

Postgres-only, like the table itself (compat/0001 is RunSQL; the DDL is skipped
on sqlite with the same pragmatism as the hypertables). `mint_*` therefore
returns None and writes nothing off Postgres instead of raising: the sqlite loop
must be able to create a farm, it just cannot prove slug resolution — which is
what the PG-only compat suite is for.

فارسی: تنها محلِ ساختِ نگاشتِ شناسه‌ها — همان قالبِ دستورِ پشتیبان‌گیری، اما در
همان تراکنشِ ساختِ مزرعه/سالن؛ وگرنه فلاتر مزرعهٔ تازه را ۴۰۴ می‌بیند.
"""

from apps.compat.models import LegacySlugMap


def slug_table_available() -> bool:
    """True on Postgres (where compat/0001 created the table), False on sqlite.

    One predicate for both callers: the write path skips minting, the backfill
    command says so out loud instead of dying with `no such table`.
    """
    from django.db import connection

    return connection.vendor == "postgresql"


def org_slug(org_id) -> str:
    """`org-<first 8 of the uuid>` — the command's documented value."""
    return f"org-{str(org_id).split('-')[0]}"


def farm_slug(farm_id) -> str:
    return f"farm-{farm_id}"


def saloon_slug(saloon_id) -> str:
    """Flutter `device` == backend saloon (spec §1.3)."""
    return f"device-{saloon_id}"


def _mint(slug: str, *, kind: str, org_id, farm_id=None, saloon_id=None):
    """Idempotent row insert; None when the table cannot exist (non-Postgres).

    `get_or_create` on the slug PK keeps re-runs harmless (the command's
    original contract) and keeps a re-created topology row from colliding with
    its own earlier slug.
    """
    if not slug_table_available():
        return None
    row, _ = LegacySlugMap.objects.get_or_create(
        slug=slug,
        defaults={
            "kind": kind,
            "org_id": org_id,
            "farm_id": farm_id,
            "saloon_id": saloon_id,
        },
    )
    return row.slug


def mint_org_slug(org):
    return _mint(org_slug(org.org_id), kind="org", org_id=org.org_id)


def mint_farm_slug(farm):
    """Mint the farm row AND (when known) the org row it hangs off."""
    if getattr(farm, "org_id", None):
        mint_org_slug(farm.org)
    return _mint(
        farm_slug(farm.pk), kind="farm", org_id=farm.org_id, farm_id=farm.pk
    )


def mint_saloon_slug(saloon):
    """Mint the saloon (`device`) row; the farm/org rows come with it.

    A `device` slug whose parent farm has no slug would still 404 one hop
    earlier at the resolver, so all three rows are minted together.
    """
    if saloon.farm_id:
        mint_farm_slug(saloon.farm)
    return _mint(
        saloon_slug(saloon.pk),
        kind="device",
        org_id=saloon.org_id,
        farm_id=saloon.farm_id,
        saloon_id=saloon.pk,
    )
