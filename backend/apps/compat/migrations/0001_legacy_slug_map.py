"""legacy_slug_map — the compat layer's ONLY schema addition (M6-09).

RunSQL with the spec §2 DDL VERBATIM (docs/api-compat-flutter-v1.md): the
CHECK constraint and both indexes are pure SQL, and the table must exist even
under --nomigrations test synthesis. Postgres-only (the project's test matrix
runs the compat suite on PG; the sqlite quick loop skips it with the same
pragmatism as the hypertable DDL).
"""

import django.db.models
from django.db import migrations, models

SQL = """
CREATE TABLE IF NOT EXISTS legacy_slug_map (
  slug       TEXT PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('org','farm','device')),
  org_id     UUID NOT NULL,
  farm_id    INTEGER NULL,
  saloon_id  INTEGER NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS legacy_slug_map_farm_id_idx ON legacy_slug_map (farm_id);
CREATE INDEX IF NOT EXISTS legacy_slug_map_saloon_id_idx ON legacy_slug_map (saloon_id);
"""

REVERSE_SQL = "DROP TABLE IF EXISTS legacy_slug_map;"


class Migration(migrations.Migration):
    dependencies = []

    operations = [
        # State registration for the unmanaged model: records LegacySlugMap in
        # the migration graph so makemigrations --check stays clean (the DDL
        # itself comes from the RunSQL below — Django emits no SQL for an
        # unmanaged CreateModel).
        migrations.CreateModel(
            name="LegacySlugMap",
            fields=[
                ("slug", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("kind", models.CharField(max_length=10)),
                ("org_id", models.UUIDField()),
                ("farm_id", models.IntegerField(blank=True, null=True)),
                ("saloon_id", models.IntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(db_default=models.functions.Now(), null=True)),
            ],
            options={
                "db_table": "legacy_slug_map",
                "managed": False,
            },
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
