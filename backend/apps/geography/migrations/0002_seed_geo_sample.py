"""
Seed: provinces/cities/villages SAMPLE (TASKS M1-07).

فارسی: نمونهٔ جغرافیایی — داکیومنت فقط DDL می‌دهد و هیچ سطر بذری ندارد؛ این
نمونهٔ کمینهٔ ساخت‌محور است، نه دادهٔ مستند.

NOTE — BUILD-PROVIDED, NOT FROM DOCS: database.md defines the geo tables but
contains NO INSERT rows for them (verified by grep + knowledge-base tables.json).
This minimal Iranian sample exists so farms can reference real FK targets in
dev/CI and the installer location-tree has something to render. Idempotent via
the doc's UNIQUE constraints. Replace with a full reference dataset when the
owner supplies one.
"""

from django.db import migrations

SQL = """
INSERT INTO provinces (name) VALUES ('اصفهان'), ('مازندران'), ('خراسان رضوی')
ON CONFLICT DO NOTHING;

INSERT INTO cities (province_id, name)
SELECT p.id, v.city_name FROM provinces p
JOIN (VALUES ('اصفهان', 'کاشان'), ('مازندران', 'ساری'), ('خراسان رضوی', 'مشهد')) AS v(prov_name, city_name)
  ON p.name = v.prov_name
ON CONFLICT DO NOTHING;

INSERT INTO villages (city_id, name)
SELECT c.id, v.village_name FROM cities c
JOIN (VALUES ('کاشان', 'جوآباد'), ('ساری', 'کندا'), ('مشهد', 'دهنو')) AS v(city_name, village_name)
  ON c.name = v.city_name
ON CONFLICT DO NOTHING;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("geography", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=SQL, reverse_sql=migrations.RunSQL.noop),
    ]
