import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class RecalculationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.engineer = Actor("eng", "engineer")
        self.inspector = Actor("insp", "inspector")
        self.operator = Actor("op", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data, actor=None):
        return self.service.create(actor or self.admin, kind, data)

    def act(self, entity, action, data=None, version=None, actor=None):
        return self.service.transition(actor or self.admin, entity["id"], action, data or {}, version)

    def setup_base(self, telemetry_revision=1, telemetry_value=10, metric="pressure"):
        station = self.create("station", {"name": "S", "region": "R"})
        asset = self.create("asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "X", "last_seen": "2026-09-27T09:00:00Z"})
        telemetry = self.create(
            "telemetry",
            {"asset_id": asset["id"], "metric": metric, "value": telemetry_value, "observed_at": "2026-09-27T09:00:00Z", "revision": telemetry_revision},
        )
        incident = self.create(
            "incident",
            {"station_id": station["id"], "asset_id": asset["id"], "kind": "link_loss", "severity": "low", "summary": "x"},
        )
        return station, asset, telemetry, incident

    def test_recovery_plan_records_telemetry_revision(self):
        station, asset, telemetry, incident = self.setup_base()
        action = self.create("recovery_action", {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k1"})
        self.assertEqual(action["data"]["telemetry_revision"], 1)
        self.assertEqual(action["data"]["telemetry_id"], telemetry["id"])

    def test_telemetry_revise_invalidates_unexecuted_plan(self):
        station, asset, telemetry, incident = self.setup_base()
        action = self.create("recovery_action", {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k1"})
        self.assertEqual(action["status"], "proposed")
        telemetry = self.act(telemetry, "revise", {"value": 20, "revision": 2})
        action = self.service.get(action["id"])
        self.assertEqual(action["status"], "pending_confirmation")
        # plan is still based on the revision it was generated from
        self.assertEqual(action["data"]["telemetry_revision"], 1)

    def test_executed_actions_kept_on_revise(self):
        station, asset, telemetry, incident = self.setup_base()
        action = self.create("recovery_action", {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k1"})
        action = self.act(action, "approve", {})
        action = self.act(action, "start", {})
        action = self.act(action, "succeed", {"outcome": "ok"})
        self.assertEqual(action["status"], "succeeded")
        telemetry = self.act(telemetry, "revise", {"value": 20, "revision": 2})
        action = self.service.get(action["id"])
        self.assertEqual(action["status"], "succeeded")

    def test_same_asset_kind_no_duplicate_active_incident(self):
        station, asset, telemetry, incident = self.setup_base()
        with self.assertRaises(ConflictError):
            self.create("incident", {"station_id": station["id"], "asset_id": asset["id"], "kind": "link_loss", "severity": "high", "summary": "y"})
        # different kind is allowed
        incident2 = self.create("incident", {"station_id": station["id"], "asset_id": asset["id"], "kind": "power_loss", "severity": "high", "summary": "y"})
        self.assertIsNotNone(incident2["id"])
        # different asset is allowed
        asset2 = self.create("asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "Y", "last_seen": "2026-09-27T09:00:00Z"})
        incident3 = self.create("incident", {"station_id": station["id"], "asset_id": asset2["id"], "kind": "link_loss", "severity": "high", "summary": "z"})
        self.assertIsNotNone(incident3["id"])

    def test_station_only_incidents_do_not_conflict(self):
        station = self.create("station", {"name": "S", "region": "R"})
        incident1 = self.create("incident", {"station_id": station["id"], "kind": "link_loss", "severity": "low", "summary": "a"})
        incident2 = self.create("incident", {"station_id": station["id"], "kind": "link_loss", "severity": "low", "summary": "b"})
        self.assertIsNotNone(incident1["id"])
        self.assertIsNotNone(incident2["id"])

    def test_concurrent_handlers_one_wins(self):
        station, asset, telemetry, incident = self.setup_base()
        v = incident["version"]
        incident_a = self.act(incident, "diagnose", {}, version=v, actor=self.operator)
        self.assertEqual(incident_a["status"], "diagnosing")
        # second handler submits with stale version → revision conflict
        with self.assertRaises(ConflictError):
            self.act(incident, "diagnose", {}, version=v, actor=self.engineer)

    def test_inspector_overreach_rejected_state_preserved(self):
        station, asset, telemetry, incident = self.setup_base()
        action = self.create("recovery_action", {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k1"})
        with self.assertRaises(PermissionDenied):
            self.act(action, "approve", {}, actor=self.inspector)
        action = self.service.get(action["id"])
        self.assertEqual(action["status"], "proposed")

    def test_engineer_reconfirms_pending_plan(self):
        station, asset, telemetry, incident = self.setup_base()
        action = self.create("recovery_action", {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k1"})
        telemetry = self.act(telemetry, "revise", {"value": 20, "revision": 2})
        action = self.service.get(action["id"])
        self.assertEqual(action["status"], "pending_confirmation")
        with self.assertRaises(PermissionDenied):
            self.act(action, "approve", {}, actor=self.inspector)
        action = self.act(action, "reconfirm", {}, actor=self.engineer)
        self.assertEqual(action["status"], "approved")

    def test_retry_failed_action(self):
        station, asset, telemetry, incident = self.setup_base()
        action = self.create("recovery_action", {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k1"})
        action = self.act(action, "approve", {})
        action = self.act(action, "start", {})
        action = self.act(action, "fail", {})
        self.assertEqual(action["status"], "failed")
        action = self.act(action, "retry", {})
        self.assertEqual(action["status"], "proposed")
        # the failed attempt is preserved in the audit trail
        audit = self.service.audit_log(action["id"])
        self.assertIn("fail", [a["action"] for a in audit])
        self.assertIn("retry", [a["action"] for a in audit])

    def test_retry_is_idempotent(self):
        station, asset, telemetry, incident = self.setup_base()
        action = self.create("recovery_action", {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k1"})
        action = self.act(action, "approve", {})
        action = self.act(action, "start", {})
        action = self.act(action, "fail", {})
        action = self.act(action, "retry", {})
        self.assertEqual(action["status"], "proposed")
        # duplicate retry returns the existing result without creating a new version bump
        again = self.act(action, "retry", {})
        self.assertEqual(again["status"], "proposed")
        self.assertEqual(again["version"], action["version"])

    def test_duplicate_submission_returns_existing(self):
        station, asset, telemetry, incident = self.setup_base()
        action1 = self.service.create(
            self.admin, "recovery_action",
            {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k1"},
            idempotency_key="idem-1",
        )
        action2 = self.service.create(
            self.admin, "recovery_action",
            {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k1"},
            idempotency_key="idem-1",
        )
        self.assertEqual(action1["id"], action2["id"])

    def test_severity_recalculated_on_revise(self):
        station, asset, telemetry, incident = self.setup_base(telemetry_value=10, metric="pressure")
        self.assertEqual(incident["data"]["severity"], "low")
        telemetry = self.act(telemetry, "revise", {"value": 95, "revision": 2})
        incident = self.service.get(incident["id"])
        self.assertEqual(incident["data"]["severity"], "critical")

    def test_mission_recalculated_on_plan_invalidation(self):
        station, asset, telemetry, incident = self.setup_base()
        action = self.create("recovery_action", {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k1"})
        mission = self.create(
            "mission",
            {"station_id": station["id"], "purpose": "repair", "window_start": "2026-10-01", "window_end": "2026-10-02", "recovery_action_id": action["id"]},
        )
        mission = self.act(mission, "approve", {}, actor=self.engineer)
        self.assertEqual(mission["status"], "approved")
        telemetry = self.act(telemetry, "revise", {"value": 20, "revision": 2})
        mission = self.service.get(mission["id"])
        self.assertEqual(mission["status"], "planned")

    def test_recalculate_method_returns_summary(self):
        station, asset, telemetry, incident = self.setup_base()
        action = self.create("recovery_action", {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k1"})
        telemetry = self.act(telemetry, "revise", {"value": 20, "revision": 2})
        result = self.service.recalculate(self.admin, telemetry)
        self.assertIn("incidents", result)
        self.assertIn("recovery_actions", result)
        self.assertIn("missions", result)
        # idempotent: calling again does not change the pending plan
        action = self.service.get(action["id"])
        self.assertEqual(action["status"], "pending_confirmation")


if __name__ == "__main__":
    unittest.main()
