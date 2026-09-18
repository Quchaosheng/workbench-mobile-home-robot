"""Issue #170: bounded, deterministic alert derivation from health snapshots.

Everything here uses a fixed monotonic clock and synthetic metric values. There
are no sleeps and no wall-clock reads, so a debounce assertion is exact.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs" / "application"))

from workbench.application.alerts import (
    ALERT_RULES,
    ALERT_RULES_VERSION,
    MAX_ACTIVE_ALERTS,
    MAX_HISTORY_ENTRIES,
    AlertCondition,
    AlertError,
    AlertPolicy,
    AlertSeverity,
    AlertState,
    AlertTracker,
    derive_alerts,
    overall_status,
    summarize_alerts,
)
from workbench.application.monitoring import HealthSnapshotCollector

BASE = 100.0


def collector(at: float = BASE, *, complete: bool = True) -> HealthSnapshotCollector:
    c = HealthSnapshotCollector()
    if complete:
        for spec in c.metrics.registry.specs():
            value = next(
                (
                    allowed
                    for allowed in spec.allowed_values
                    if allowed not in spec.fault_values and allowed not in spec.degraded_values
                ),
                None,
            )
            if value is None:
                if spec.value_type == "bool":
                    value = True
                elif spec.value_type == "str":
                    value = "none"
                elif spec.value_type == "int":
                    value = 5
                else:
                    value = 5.0
            c.record(spec.name, value, source=spec.source, observed_at=at)
    return c


def snapshot(
    *,
    at: float = BASE,
    complete: bool = True,
    faults: dict[str, object] | None = None,
    observed_at: float | None = None,
):
    """Build one snapshot observed at ``at`` (or ``observed_at``) and collected at ``at``."""
    observed = at if observed_at is None else observed_at
    # Healthy values are stamped just before the faulted ones so a metric can be
    # overwritten without tripping the collector's same-timestamp conflict guard.
    c = collector(observed - 0.01, complete=complete)
    for name, value in (faults or {}).items():
        c.record(name, value, source=c.metrics.registry.get(name).source, observed_at=observed)
    return c.snapshot(collected_at=at)


def healthy_snapshot(at: float = BASE):
    return snapshot(at=at)


class RuleTableTests(unittest.TestCase):
    def test_every_rule_is_unique_and_named(self) -> None:
        metrics = [rule.metric for rule in ALERT_RULES]
        self.assertEqual(len(metrics), len(set(metrics)), "one rule per metric")
        for rule in ALERT_RULES:
            with self.subTest(metric=rule.metric):
                self.assertTrue(rule.summary.strip())

    def test_critical_rules_cover_the_required_safety_domains(self) -> None:
        critical = {rule.metric for rule in ALERT_RULES if rule.severity is AlertSeverity.CRITICAL}
        for required in (
            "safety.estop_channels_ok",
            "safety.mcu_watchdog_ok",
            "safety.contactor_permission",
            "power.bms_state",
            "can.bus_state",
            "can.link_ok",
            "motion.controller_ok",
            "motion.stop_state",
            "nav.localization_ok",
            "perception.fresh",
            "event_store.integrity_ok",
        ):
            self.assertIn(required, critical)

    def test_a_rule_matching_nothing_is_refused(self) -> None:
        from workbench.application.alerts import AlertRule

        with self.assertRaises(AlertError):
            AlertRule("x.y", AlertCondition.SOURCE_FAULT, AlertSeverity.INFO)
        with self.assertRaises(AlertError):
            AlertRule("", AlertCondition.SOURCE_FAULT, AlertSeverity.INFO, fault_values=(1,))
        with self.assertRaises(AlertError):
            AlertRule("x.y", "not-a-condition", AlertSeverity.INFO, fault_values=(1,))  # type: ignore[arg-type]
        with self.assertRaises(AlertError):
            AlertRule("x.y", AlertCondition.SOURCE_FAULT, "urgent", fault_values=(1,))  # type: ignore[arg-type]


class DerivationTests(unittest.TestCase):
    def test_a_complete_fresh_snapshot_produces_no_alerts(self) -> None:
        self.assertEqual(derive_alerts(healthy_snapshot()), ())

    def test_a_faulted_critical_metric_produces_its_named_condition(self) -> None:
        cases = {
            "safety.estop_channels_ok": (False, AlertCondition.ESTOP_UNAVAILABLE, AlertSeverity.CRITICAL),
            "safety.mcu_watchdog_ok": (False, AlertCondition.WATCHDOG_LOSS, AlertSeverity.CRITICAL),
            "safety.contactor_permission": (False, AlertCondition.CONTACTOR_DENIED, AlertSeverity.CRITICAL),
            "power.bms_state": ("FAULT_LATCHED", AlertCondition.BMS_FAULT, AlertSeverity.CRITICAL),
            "can.bus_state": ("bus-off", AlertCondition.CAN_BUS_OFF, AlertSeverity.CRITICAL),
            "can.link_ok": (False, AlertCondition.CAN_LINK_LOSS, AlertSeverity.CRITICAL),
            "motion.controller_ok": (False, AlertCondition.CONTROLLER_FAULT, AlertSeverity.CRITICAL),
            "motion.stop_state": ("fault", AlertCondition.STOP_FAULT, AlertSeverity.CRITICAL),
            "nav.localization_ok": (False, AlertCondition.LOCALIZATION_STALE, AlertSeverity.CRITICAL),
            "perception.fresh": (False, AlertCondition.PERCEPTION_STALE, AlertSeverity.CRITICAL),
            "event_store.integrity_ok": (False, AlertCondition.EVENT_STORE_INTEGRITY, AlertSeverity.CRITICAL),
            "backend.available": (False, AlertCondition.BACKEND_UNAVAILABLE, AlertSeverity.WARNING),
        }
        for metric, (value, condition, severity) in cases.items():
            with self.subTest(metric=metric):
                alerts = derive_alerts(snapshot(faults={metric: value}))
                matching = [alert for alert in alerts if alert.metric == metric]
                self.assertEqual(len(matching), 1, alerts)
                self.assertEqual(matching[0].condition, condition)
                self.assertEqual(matching[0].severity, severity)

    def test_a_degraded_value_is_at_most_one_step_below_its_rule(self) -> None:
        # DERATE on a critical rule degrades to a warning, not to info.
        bms = derive_alerts(snapshot(faults={"power.bms_state": "DERATE"}))
        self.assertEqual([alert.severity for alert in bms], [AlertSeverity.WARNING])
        # A warning-level rule with a degraded value stays a warning, never info.
        disk = derive_alerts(snapshot(faults={"compute.disk_free_bytes": 0}))
        self.assertEqual([alert.severity for alert in disk], [AlertSeverity.WARNING])

    def test_missing_and_stale_inputs_are_their_own_conditions(self) -> None:
        missing = derive_alerts(snapshot(complete=False))
        self.assertTrue(missing, "an empty snapshot must not be silent")
        self.assertEqual({alert.condition for alert in missing}, {AlertCondition.SOURCE_MISSING})
        self.assertFalse(any(alert.severity is AlertSeverity.INFO for alert in missing))

        # Collected long after the observation: every metric is stale.
        stale = derive_alerts(snapshot(at=100_000.0, observed_at=BASE))
        self.assertEqual({alert.condition for alert in stale}, {AlertCondition.SOURCE_STALE})

    def test_missing_data_is_never_reported_as_healthy_or_zero(self) -> None:
        document = snapshot(complete=False)
        alerts = derive_alerts(document)
        self.assertGreater(len(alerts), 0)
        for alert in alerts:
            self.assertIsNone(alert.observed_value)
            self.assertNotEqual(alert.severity, AlertSeverity.INFO)

    def test_alerts_name_their_evidence_and_are_deterministic(self) -> None:
        document = snapshot(faults={"can.bus_state": "bus-off"})
        first = derive_alerts(document)
        second = derive_alerts(document)
        self.assertEqual(first, second)
        for alert in first:
            self.assertTrue(alert.evidence_ref.startswith("health-snapshot://"))
            self.assertEqual(alert.rules_version, ALERT_RULES_VERSION)
            self.assertGreaterEqual(alert.count, 1)

    def test_derivation_refuses_a_non_snapshot(self) -> None:
        for bad in (None, "snapshot", 7):
            with self.subTest(value=bad), self.assertRaises(AlertError):
                derive_alerts(bad)  # type: ignore[arg-type]


class TrackerTests(unittest.TestCase):
    def test_a_condition_must_persist_before_it_opens(self) -> None:
        tracker = AlertTracker(AlertPolicy(open_after_s=2.0))
        first = tracker.observe(snapshot(at=BASE, faults={"can.link_ok": False}))
        self.assertEqual(tracker.active(), ())
        # Same condition one second later: still inside the debounce window.
        tracker.observe(snapshot(at=BASE + 1.0, faults={"can.link_ok": False}))
        self.assertEqual(tracker.active(), ())
        # Two seconds after it was first seen: it opens.
        tracker.observe(snapshot(at=BASE + 2.0, faults={"can.link_ok": False}))
        self.assertEqual([alert.condition for alert in tracker.active()], [AlertCondition.CAN_LINK_LOSS])
        self.assertTrue(first, "the changed set reports the pending condition")

    def test_a_cleared_condition_is_held_open_for_the_clear_window(self) -> None:
        tracker = AlertTracker(AlertPolicy(clear_after_s=2.0))
        tracker.observe(snapshot(at=BASE, faults={"can.link_ok": False}))
        self.assertEqual(len(tracker.active()), 1)
        # Recovery at +1s: still active until the clear window elapses.
        tracker.observe(healthy_snapshot(at=BASE + 1.0))
        self.assertEqual(len(tracker.active()), 1)
        tracker.observe(healthy_snapshot(at=BASE + 3.0))
        self.assertEqual(tracker.active(), ())
        self.assertEqual(len(tracker.history()), 1)
        self.assertEqual(tracker.history()[0].state, AlertState.CLEARED)

    def test_a_single_noisy_sample_never_opens_an_alert(self) -> None:
        """Flapping: one blip must not page, with hysteresis on both edges."""
        tracker = AlertTracker(AlertPolicy(open_after_s=3.0, clear_after_s=3.0))
        tracker.observe(healthy_snapshot(at=BASE))
        tracker.observe(snapshot(at=BASE + 1.0, faults={"can.bus_state": "bus-off"}))
        tracker.observe(healthy_snapshot(at=BASE + 2.0))
        self.assertEqual(tracker.active(), ())
        self.assertEqual(tracker.history(), ())

    def test_counts_grow_while_a_condition_persists(self) -> None:
        tracker = AlertTracker()
        for step in range(4):
            tracker.observe(snapshot(at=BASE + step, faults={"can.link_ok": False}))
        active = tracker.active()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].count, 4)
        self.assertEqual(active[0].first_seen_at, BASE)

    def test_out_of_order_and_non_finite_snapshots_are_refused(self) -> None:
        tracker = AlertTracker()
        tracker.observe(healthy_snapshot(at=BASE + 5.0))
        with self.assertRaises(AlertError):
            tracker.observe(healthy_snapshot(at=BASE))
        with self.assertRaises(AlertError):
            tracker.observe("not-a-snapshot")  # type: ignore[arg-type]

    def test_history_is_bounded_by_count(self) -> None:
        tracker = AlertTracker(AlertPolicy(max_history=3))
        for step in range(10):
            at = BASE + step * 2.0
            tracker.observe(snapshot(at=at, faults={"can.link_ok": False}))
            tracker.observe(healthy_snapshot(at=at + 0.6))
        self.assertLessEqual(len(tracker.history()), 3)
        # The retained entries are the newest ones.
        last = tracker.history()[-1]
        self.assertGreaterEqual(last.last_seen_at, BASE + 14.0)

    def test_history_is_bounded_by_age(self) -> None:
        tracker = AlertTracker(AlertPolicy(history_age_s=5.0))
        tracker.observe(snapshot(at=BASE, faults={"can.link_ok": False}))
        tracker.observe(healthy_snapshot(at=BASE + 1.0))
        self.assertEqual(len(tracker.history()), 1)
        # A much later snapshot ages the entry out.
        tracker.observe(healthy_snapshot(at=BASE + 100.0))
        self.assertEqual(tracker.history(), ())

    def test_explicit_clear_records_history_once(self) -> None:
        tracker = AlertTracker()
        tracker.observe(snapshot(at=BASE, faults={"can.link_ok": False}))
        alert_id = tracker.active()[0].alert_id
        self.assertTrue(tracker.clear(alert_id))
        self.assertFalse(tracker.clear(alert_id))
        self.assertEqual(tracker.active(), ())
        self.assertEqual(len(tracker.history()), 1)

    def test_policy_limits_are_validated(self) -> None:
        for kwargs in (
            {"open_after_s": -1},
            {"clear_after_s": -1},
            {"open_after_s": float("nan")},
            {"max_history": 0},
            {"max_history": MAX_HISTORY_ENTRIES + 1},
            {"history_age_s": 10**9},
            {"max_history": True},
        ):
            with self.subTest(**kwargs), self.assertRaises(AlertError):
                AlertPolicy(**kwargs)  # type: ignore[arg-type]
        with self.assertRaises(AlertError):
            AlertTracker("not-a-policy")  # type: ignore[arg-type]

    def test_as_dict_is_serializable_and_versioned(self) -> None:
        import json

        tracker = AlertTracker()
        tracker.observe(snapshot(at=BASE, faults={"can.bus_state": "bus-off"}))
        document = tracker.as_dict()
        self.assertEqual(document["rules_version"], ALERT_RULES_VERSION)
        json.dumps(document)  # must not raise
        self.assertEqual(len(document["active"]), 1)


class SummaryTests(unittest.TestCase):
    def test_summary_counts_by_severity_and_condition(self) -> None:
        alerts = derive_alerts(snapshot(faults={"can.link_ok": False, "backend.available": False}))
        summary = summarize_alerts(alerts)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_severity"]["critical"], 1)
        self.assertEqual(summary["by_severity"]["warning"], 1)
        self.assertEqual(summary["by_condition"]["can_link_loss"], 1)

    def test_summary_refuses_non_alert_values(self) -> None:
        with self.assertRaises(AlertError):
            summarize_alerts(["nope"])  # type: ignore[list-item]

    def test_overall_status_never_says_healthy_when_a_critical_alert_is_active(self) -> None:
        document = snapshot(faults={"safety.estop_channels_ok": False})
        alerts = derive_alerts(document)
        self.assertEqual(overall_status(document, alerts), "fault")

        warning = derive_alerts(snapshot(faults={"backend.available": False}))
        self.assertEqual(overall_status(snapshot(faults={"backend.available": False}), warning), "degraded")

        self.assertEqual(overall_status(healthy_snapshot(), ()), "healthy")

    def test_unknown_is_preserved_when_there_are_no_alerts_but_state_is_unknown(self) -> None:
        document = snapshot(complete=False)
        # With alerts present the missing input already drives the status; the
        # guard exists so a future rule table that omits a metric cannot turn an
        # unknown snapshot into a healthy one.
        self.assertEqual(overall_status(document, ()), "unknown")

    def test_alert_count_is_bounded(self) -> None:
        self.assertLessEqual(len(derive_alerts(snapshot(complete=False))), MAX_ACTIVE_ALERTS)


if __name__ == "__main__":
    unittest.main()
