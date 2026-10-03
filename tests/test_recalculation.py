import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class RecalculationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine()
        )
        self.admin = Actor("admin", "admin")
        self.engineer = Actor("eng-1", "engineer")
        self.op_a = Actor("op-a", "operator")
        self.op_b = Actor("op-b", "operator")
        self.field = Actor("patrol-1", "field")

    def tearDown(self):
        self.tmp.cleanup()

    def _station_asset(self):
        station = self.service.create(
            self.admin, "station", {"name": "OSN-01", "region": "East"}
        )
        asset = self.service.create(
            self.admin,
            "asset",
            {
                "station_id": station["id"],
                "asset_type": "sensor",
                "serial_no": "S-1",
                "last_seen": "2026-10-03T08:00:00Z",
            },
        )
        return station, asset

    def _telemetry(self, asset, value=30, revision=1):
        return self.service.create(
            self.admin,
            "telemetry",
            {
                "asset_id": asset["id"],
                "metric": "pressure",
                "value": value,
                "observed_at": "2026-10-03T08:00:00Z",
                "revision": revision,
            },
        )

    def _incident(self, station, asset, telemetry, actor=None):
        actor = actor or self.op_a
        return self.service.create(
            actor,
            "incident",
            {
                "station_id": station["id"],
                "asset_id": asset["id"],
                "kind": "link_loss",
                "summary": "no data",
                "basis_telemetry_id": telemetry["id"],
            },
        )

    # 1. 迟到修订驱动的整套重算流程
    def test_late_revision_recalculates_severity_and_invalidates_unexecuted_plans(self):
        station, asset = self._station_asset()
        telemetry = self._telemetry(asset, value=30, revision=1)

        incident = self._incident(station, asset, telemetry)
        # 严重度按所采用的遥测修订重算，方案记下采用的修订号
        self.assertEqual(incident["data"]["severity"], "low")
        self.assertEqual(incident["data"]["basis_revision"], 1)

        for action in ("diagnose", "plan_recovery", "start_recovery"):
            incident = self.service.transition(self.engineer, incident["id"], action)

        executed = self.service.create(
            self.op_a,
            "recovery_action",
            {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "done-1"},
        )
        self.assertEqual(executed["data"]["basis_revision"], 1)
        executed = self.service.transition(self.engineer, executed["id"], "approve")
        executed = self.service.transition(self.op_a, executed["id"], "start")
        executed = self.service.transition(
            self.op_a, executed["id"], "succeed", {"outcome": "online"}
        )

        pending = self.service.create(
            self.op_a,
            "recovery_action",
            {"incident_id": incident["id"], "action_type": "switch_backup", "dedupe_key": "plan-1"},
        )
        pending = self.service.transition(self.engineer, pending["id"], "approve")

        mission = self.service.create(
            self.admin,
            "mission",
            {
                "station_id": station["id"],
                "incident_id": incident["id"],
                "purpose": "replace sensor",
                "window_start": "2026-10-05T00:00:00Z",
                "window_end": "2026-10-06T00:00:00Z",
            },
        )
        self.assertEqual(mission["data"]["basis_revision"], 1)

        # 科考船迟到回传：更高修订号才接受，并触发重算
        telemetry = self.service.transition(
            self.op_a,
            telemetry["id"],
            "revise",
            {"value": 90, "revision": 2, "observed_at": "2026-10-03T09:00:00Z"},
        )
        self.assertEqual(telemetry["data"]["revision"], 2)

        incident = self.service.get(incident["id"])
        self.assertEqual(incident["data"]["severity"], "critical")
        self.assertEqual(incident["data"]["basis_revision"], 2)

        # 未执行方案立刻失效退回待确认
        pending = self.service.get(pending["id"])
        self.assertEqual(pending["status"], "proposed")
        self.assertTrue(pending["data"]["stale"])
        self.assertEqual(pending["data"]["invalidated_by_revision"], 2)

        mission = self.service.get(mission["id"])
        self.assertEqual(mission["status"], "planned")
        self.assertTrue(mission["data"]["stale"])

        # 已执行动作留档，不被修订改写
        executed = self.service.get(executed["id"])
        self.assertEqual(executed["status"], "succeeded")
        self.assertEqual(executed["data"]["basis_revision"], 1)
        self.assertNotIn("stale", executed["data"])

        # 失效方案未被工程人员重新确认前不能推进
        with self.assertRaises(ConflictError):
            self.service.transition(self.engineer, pending["id"], "approve")
        with self.assertRaises(ConflictError):
            self.service.transition(self.engineer, mission["id"], "approve")

        # 值班员不能重新确认，必须工程人员重新确认才能改变状态
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.op_b, pending["id"], "reconfirm")
        reconfirmed = self.service.transition(self.engineer, pending["id"], "reconfirm")
        self.assertEqual(reconfirmed["status"], "proposed")
        self.assertNotIn("stale", reconfirmed["data"])
        self.assertEqual(reconfirmed["data"]["basis_revision"], 2)
        self.assertEqual(reconfirmed["data"]["reconfirmed_by"], "eng-1")
        approved = self.service.transition(self.engineer, pending["id"], "approve")
        self.assertEqual(approved["status"], "approved")

        mission = self.service.transition(self.engineer, mission["id"], "reconfirm")
        self.assertNotIn("stale", mission["data"])
        self.assertEqual(mission["data"]["basis_revision"], 2)

        audit = {entry["action"] for entry in self.service.audit_log()}
        self.assertIn("recalculate", audit)
        self.assertIn("invalidate", audit)

    def test_running_action_and_completed_mission_are_left_archived(self):
        station, asset = self._station_asset()
        telemetry = self._telemetry(asset, value=30)
        incident = self._incident(station, asset, telemetry)
        for action in ("diagnose", "plan_recovery", "start_recovery"):
            incident = self.service.transition(self.engineer, incident["id"], action)

        running = self.service.create(
            self.op_a,
            "recovery_action",
            {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "r"},
        )
        running = self.service.transition(self.engineer, running["id"], "approve")
        running = self.service.transition(self.op_a, running["id"], "start")

        mission = self.service.create(
            self.admin,
            "mission",
            {
                "station_id": station["id"],
                "incident_id": incident["id"],
                "purpose": "inspect",
                "window_start": "2026-10-05T00:00:00Z",
                "window_end": "2026-10-06T00:00:00Z",
            },
        )
        mission = self.service.transition(self.engineer, mission["id"], "approve")
        mission = self.service.transition(self.op_a, mission["id"], "depart")
        mission = self.service.transition(
            self.admin, mission["id"], "complete", {"report": "done"}
        )

        self.service.transition(
            self.op_a, telemetry["id"], "revise", {"value": 95, "revision": 2}
        )

        running = self.service.get(running["id"])
        self.assertEqual(running["status"], "running")
        self.assertNotIn("stale", running["data"])
        mission = self.service.get(mission["id"])
        self.assertEqual(mission["status"], "completed")
        self.assertNotIn("stale", mission["data"])

    def test_reconfirm_without_stale_is_rejected(self):
        station, asset = self._station_asset()
        telemetry = self._telemetry(asset)
        incident = self._incident(station, asset, telemetry)
        action = self.service.create(
            self.op_a,
            "recovery_action",
            {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k"},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(self.engineer, action["id"], "reconfirm")

    def test_lower_revision_is_rejected_and_state_is_preserved(self):
        station, asset = self._station_asset()
        telemetry = self._telemetry(asset, value=30, revision=2)
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.op_a, telemetry["id"], "revise", {"revision": 1, "value": 90}
            )
        untouched = self.service.get(telemetry["id"])
        self.assertEqual(untouched["data"]["revision"], 2)
        self.assertEqual(untouched["data"]["value"], 30)
        self.assertNotIn("late_revision", untouched["data"])

    # 2. 同一资产同一故障类型不能另开活动事件
    def test_same_asset_and_kind_cannot_have_two_active_incidents(self):
        station, asset = self._station_asset()
        telemetry = self._telemetry(asset)
        self._incident(station, asset, telemetry)
        with self.assertRaises(ConflictError):
            self._incident(station, asset, telemetry, actor=self.op_b)
        incidents = self.service.list("incident")
        self.assertEqual(len(incidents), 1)

    # 3. 两个值班员并发提交：只一方生效，后到者看到修订冲突
    def test_concurrent_incident_submission_only_one_wins(self):
        station, asset = self._station_asset()
        telemetry = self._telemetry(asset)
        payload = {
            "station_id": station["id"],
            "asset_id": asset["id"],
            "kind": "link_loss",
            "summary": "race",
            "basis_telemetry_id": telemetry["id"],
        }
        results = []
        barrier = threading.Barrier(2)

        def submit(actor):
            barrier.wait()
            try:
                results.append(("ok", self.service.create(actor, "incident", payload)))
            except ConflictError as exc:
                results.append(("conflict", str(exc)))

        t1 = threading.Thread(target=submit, args=(self.op_a,))
        t2 = threading.Thread(target=submit, args=(self.op_b,))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1)
        self.assertEqual(sum(1 for kind, _ in results if kind == "conflict"), 1)
        self.assertEqual(len(self.service.list("incident")), 1)

    def test_concurrent_revision_only_one_wins(self):
        station, asset = self._station_asset()
        telemetry = self._telemetry(asset, value=30, revision=1)
        results = []
        barrier = threading.Barrier(2)

        def revise(revision, value):
            barrier.wait()
            try:
                updated = self.service.transition(
                    self.op_a,
                    telemetry["id"],
                    "revise",
                    {"revision": revision, "value": value},
                    expected_version=1,
                )
                results.append(("ok", updated["data"]["revision"]))
            except ConflictError:
                results.append(("conflict", None))

        t1 = threading.Thread(target=revise, args=(2, 75))
        t2 = threading.Thread(target=revise, args=(3, 95))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sum(1 for kind, _ in results if kind == "conflict"), 1)
        winner = [rev for kind, rev in results if kind == "ok"]
        self.assertEqual(len(winner), 1)
        self.assertEqual(self.service.get(telemetry["id"])["data"]["revision"], winner[0])

    def test_concurrent_recovery_action_dedupe_only_one_wins(self):
        station, asset = self._station_asset()
        telemetry = self._telemetry(asset)
        incident = self._incident(station, asset, telemetry)
        payload = {
            "incident_id": incident["id"],
            "action_type": "remote_restart",
            "dedupe_key": "restart-race",
        }
        results = []
        barrier = threading.Barrier(2)

        def submit(actor):
            barrier.wait()
            try:
                results.append(("ok", self.service.create(actor, "recovery_action", payload)))
            except ConflictError:
                results.append(("conflict", None))

        t1 = threading.Thread(target=submit, args=(self.op_a,))
        t2 = threading.Thread(target=submit, args=(self.op_b,))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1)
        self.assertEqual(len(self.service.list("recovery_action")), 1)

    # 4. 巡检员越权提交被拒绝且保留原状态
    def test_field_overreach_is_denied_without_state_change(self):
        station, asset = self._station_asset()
        telemetry = self._telemetry(asset, value=90, revision=1)
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.field,
                "incident",
                {
                    "station_id": station["id"],
                    "asset_id": asset["id"],
                    "kind": "link_loss",
                    "summary": "patrol attempt",
                    "basis_telemetry_id": telemetry["id"],
                },
            )
        self.assertEqual(self.service.list("incident"), [])

        incident = self._incident(station, asset, telemetry)
        action = self.service.create(
            self.op_a,
            "recovery_action",
            {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k"},
        )
        self.service.transition(
            self.op_a,
            telemetry["id"],
            "revise",
            {"value": 95, "revision": 2},
        )
        action = self.service.get(action["id"])
        self.assertTrue(action["data"]["stale"])

        # 巡检员既不能重新确认，也不能越权推进；原状态保留
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.field, action["id"], "reconfirm")
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.field, action["id"], "approve")
        still = self.service.get(action["id"])
        self.assertEqual(still["status"], "proposed")
        self.assertTrue(still["data"]["stale"])

    # 5. 重复提交返回已有结果；失败后重试只处理未完成对象
    def test_idempotent_retry_returns_existing_result(self):
        station, asset = self._station_asset()
        telemetry = self._telemetry(asset)
        payload = {
            "station_id": station["id"],
            "asset_id": asset["id"],
            "kind": "link_loss",
            "summary": "idem",
            "basis_telemetry_id": telemetry["id"],
        }
        first = self.service.create(self.op_a, "incident", payload, idempotency_key="submit-1")
        retry = self.service.create(self.op_a, "incident", payload, idempotency_key="submit-1")
        self.assertEqual(first["id"], retry["id"])
        self.assertEqual(len(self.service.list("incident")), 1)

        # 不带幂等键的真重复提交仍然报冲突
        with self.assertRaises(ConflictError):
            self.service.create(self.op_b, "incident", dict(payload, summary="again"))

    def test_batch_retry_only_processes_unfinished_objects(self):
        station, asset = self._station_asset()

        def run_batch(items):
            done = []
            for item in items:
                done.append(
                    self.service.create(
                        self.op_a,
                        "telemetry",
                        {
                            "asset_id": asset["id"],
                            "metric": item["metric"],
                            "value": item["value"],
                            "observed_at": "2026-10-03T08:00:00Z",
                            "revision": 1,
                        },
                        idempotency_key="batch:" + item["metric"],
                    )
                )
            return done

        items = [
            {"metric": "pressure", "value": 1},
            {"metric": "temperature", "value": 2},
            {"metric": "voltage", "value": 3},
        ]
        # 第一批在处理完前两个后“失败”
        first = run_batch(items[:2])
        self.assertEqual(len(first), 2)
        # 重试整批：已完成对象返回已有结果，第三个首次落库
        retried = run_batch(items)
        self.assertEqual(len(retried), 3)
        self.assertEqual(retried[0]["id"], first[0]["id"])
        self.assertEqual(retried[1]["id"], first[1]["id"])
        self.assertEqual(len(self.service.list("telemetry")), 3)

    def test_failed_action_can_be_resubmitted_after_terminal_state(self):
        station, asset = self._station_asset()
        telemetry = self._telemetry(asset)
        incident = self._incident(station, asset, telemetry)
        first = self.service.create(
            self.op_a,
            "recovery_action",
            {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "d"},
        )
        self.service.transition(self.engineer, first["id"], "approve")
        self.service.transition(self.op_a, first["id"], "start")
        first = self.service.transition(self.op_a, first["id"], "fail")
        self.assertEqual(first["status"], "failed")
        # 终态动作留档，同一去重键可重新开一个方案
        second = self.service.create(
            self.op_a,
            "recovery_action",
            {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "d"},
        )
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["status"], "proposed")


if __name__ == "__main__":
    unittest.main()
