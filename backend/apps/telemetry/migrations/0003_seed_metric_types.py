"""
Seed: sensor metric catalog — 8 keys VERBATIM (database.md Compartment 3).

فارسی: کاتالوگ متریک‌ها عیناً از داکیومنت؛ کلیدهای D23 از قبل می‌نشینند تا
هنگام رسیدن سنسور، ورکر بدون هیچ تغییری آن‌ها را کشف کند.

Capability-modular keys (§4h): water_l / co / power_mains / power_gen exist so
ingestion auto-discovers them the moment a farm HAS the sensor; saloons
without it never see the key. THI is deliberately NOT a row (derived metric).

Idempotent: ON CONFLICT (metric_key) DO NOTHING — safe on re-run and on any
volume where initdb-time data already exists.
"""

from django.db import migrations

SQL = """
INSERT INTO sensor_metric_types (metric_key, display_name, unit, value_type, is_tier1, icon_name) VALUES
    ('temp',     'Temperature',    'C',   'numeric', true,  'thermometer'),
    ('humidity', 'Humidity',       '%',   'numeric', false, 'droplet'),
    ('lux',      'Light Intensity','lux', 'numeric', false, 'sun'),
    ('nh3',      'Ammonia',        'ppm', 'numeric', true,  'wind'),
    ('water_l',  'Water Intake',   'L',   'numeric', false, 'droplet'),
    ('co',       'Carbon Monoxide','ppm', 'numeric', true,  'alert'),
    ('power_mains', 'Mains Power', 'bool','numeric', false, 'plug'),
    ('power_gen',   'Generator',   'bool','numeric', false, 'engine')
ON CONFLICT (metric_key) DO NOTHING;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("telemetry", "0002_timescale_hypertables"),
    ]

    operations = [
        migrations.RunSQL(sql=SQL, reverse_sql=migrations.RunSQL.noop),
    ]
