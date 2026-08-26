"""M4-00 acceptance — canonical alerts task names are registered.

WHY: Blueprint §5 declares the task names as CANONICAL ("drift is over").
Registering them in M4-00 pins the topology: beat entries, routing and tests
reference these names for the rest of the project without rename churn.
Q10: rebuild_active_state_from_db stays PARKED until M8 — it must fail LOUD
(NotImplementedError), never silently no-op.
فارسی: پذیرش M4-00 — شش نامِ کانونیکل باید روی اپ سلری ثبت شده باشند و تسکِ
پارک‌شده باید بلند شکست بخورد نه اینکه بی‌صدا هیچ کاری نکند.
"""

from django.test import TestCase

from apps.alerts import tasks as alerts_tasks


class CanonicalTaskNameTests(TestCase):
    EXPECTED = {
        "alerts.cache_daily_thresholds",
        "alerts.cache_saloon_thresholds",
        "alerts.escalate_alerts",
        "alerts.los_watchdog_sweep",
        "alerts.run_replay_alert_sweep",
        "alerts.rebuild_active_state_from_db",
    }

    def test_all_canonical_names_registered(self):
        from config.celery import app

        missing = self.EXPECTED - set(app.tasks)
        self.assertEqual(missing, set())

    def test_parked_rebuild_task_fails_loud(self):
        with self.assertRaises(NotImplementedError):
            alerts_tasks.rebuild_active_state_from_db()


class RedisKeyShapeTests(TestCase):
    def test_key_builders_shape(self):
        from apps.alerts.services import redis_keys as keys

        self.assertEqual(keys.thresholds_hash(7), "saloon:7:thresholds")
        self.assertEqual(keys.transition_grace_key(7), "threshold:transition:7")
        self.assertEqual(keys.breach_key(7, "temp"), "saloon:7:breach:temp")
        self.assertEqual(
            keys.active_alert_key(7, "nh3"), "alert:active:saloon:7:nh3"
        )
        self.assertEqual(keys.paused_key("abc"), "alert:paused:abc")
        self.assertEqual(
            keys.sensor_status_key(7, "SLOT-1"), "sensor:status:7:SLOT-1"
        )
        self.assertEqual(keys.replay_pending("GW-1"), "replay:pending:GW-1")
        self.assertEqual(keys.replay_sweeping("GW-1"), "replay:sweeping:GW-1")
