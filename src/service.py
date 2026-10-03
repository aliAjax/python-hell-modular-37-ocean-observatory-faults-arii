import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import (
    ACTIVE_INCIDENT_STATUSES,
    TERMINAL_ACTION_STATUSES,
    RuleEngine,
    recalculate_severity,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    # 重复提交（重试）返回已有结果，不重新执行。
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        self._ensure_no_active_duplicate(kind, payload)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _ensure_no_active_duplicate(self, kind, payload):
        """Pre-insert duplicate check; the unique indexes are the race-safe backstop."""
        if kind == "incident":
            asset_id = payload.get("asset_id")
            if not asset_id:
                return
            for item in self.repository.list_entities(kind="incident"):
                if (
                    item["status"] in ACTIVE_INCIDENT_STATUSES
                    and item["data"].get("asset_id") == asset_id
                    and item["data"].get("kind") == payload.get("kind")
                ):
                    # 另一值班员的活动事件已落库：后到者看到冲突。
                    raise ConflictError("active incident already exists for asset and kind")
        if kind == "recovery_action":
            dedupe_key = payload.get("dedupe_key")
            if not dedupe_key:
                return
            for item in self.repository.list_entities(kind="recovery_action"):
                if (
                    item["status"] not in TERMINAL_ACTION_STATUSES
                    and item["data"].get("dedupe_key") == dedupe_key
                ):
                    raise ConflictError(
                        "recovery action already submitted for dedupe_key %s" % dedupe_key
                    )

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch, drop = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        if entity["kind"] == "telemetry" and action == "revise":
            return self._revise_with_cascade(actor, entity, expected, next_status, patch, drop)
        merged = dict(entity["data"])
        merged.update(patch)
        for key in drop:
            merged.pop(key, None)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch, "drop": drop},
        )
        return updated

    def _revise_with_cascade(self, actor, telemetry, expected, next_status, patch, drop):
        """Apply a late telemetry revision and recalculate every dependent object.

        - incidents tied to the telemetry get severity/basis revision recomputed;
        - unexecuted recovery plans and unexecuted missions are invalidated back to
          pending confirmation;
        - executed actions / completed missions are left untouched (archived).
        All writes commit in one transaction.
        """
        asset_id = telemetry["data"].get("asset_id")
        metric = telemetry["data"].get("metric")
        new_revision = int(patch.get("revision"))
        new_value = patch.get("value", telemetry["data"].get("value"))
        dependent_ids = set()
        with self.repository.unit_of_work() as connection:
            # 乐观锁 + 单写事务：两个值班员同时修订只一方成功。
            updated = self.repository.update_entity_conn(
                connection, telemetry["id"], expected, next_status,
                self._merge_data(telemetry["data"], patch, drop),
            )
            self.repository.append_audit_conn(
                connection, telemetry["id"], actor.user_id, actor.role, "revise",
                telemetry["status"], next_status, {"patch": patch, "drop": drop},
            )
            for incident in self.repository.list_entities_conn(connection, kind="incident"):
                basis = incident["data"].get("basis_telemetry_id")
                tied = basis == telemetry["id"] or (
                    not basis
                    and incident["data"].get("asset_id") == asset_id
                    and incident["data"].get("metric") == metric
                )
                if not tied or incident["status"] in ("resolved", "closed"):
                    continue
                dependent_ids.add(incident["id"])
                new_severity = recalculate_severity(new_value)
                incident_patch = {
                    "severity": new_severity,
                    "basis_telemetry_id": telemetry["id"],
                    "basis_revision": new_revision,
                    "recalculated_from_revision": new_revision,
                    "recalculated_by": actor.user_id,
                }
                self.repository.update_entity_conn(
                    connection, incident["id"], incident["version"],
                    incident["status"], {**incident["data"], **incident_patch},
                )
                self.repository.append_audit_conn(
                    connection, incident["id"], actor.user_id, actor.role,
                    "recalculate", incident["status"], incident["status"],
                    {"reason": "telemetry_revised", "telemetry_id": telemetry["id"],
                     "revision": new_revision, "severity": new_severity},
                )
            for ra in self.repository.list_entities_conn(connection, kind="recovery_action"):
                if ra["data"].get("incident_id") not in dependent_ids:
                    continue
                self._invalidate_dependent(
                    connection, actor, ra, telemetry, new_revision,
                    "recovery_action", ("proposed", "approved"), "proposed",
                )
            for mission in self.repository.list_entities_conn(connection, kind="mission"):
                if mission["data"].get("incident_id") not in dependent_ids:
                    continue
                self._invalidate_dependent(
                    connection, actor, mission, telemetry, new_revision,
                    "mission", ("planned", "approved"), "planned",
                )
        return updated

    @staticmethod
    def _merge_data(current, patch, drop):
        merged = dict(current)
        merged.update(patch)
        for key in drop or []:
            merged.pop(key, None)
        return merged

    def _invalidate_dependent(self, connection, actor, entity, telemetry, new_revision, kind, invalidable, pending):
        """未执行（尚未开始/出海）对象退回待确认；已执行或进行中的对象留档不动。"""
        if entity["status"] not in invalidable:
            return
        patch = {
            "stale": True,
            "invalidated_by_revision": new_revision,
            "basis_telemetry_id": telemetry["id"],
        }
        self.repository.update_entity_conn(
            connection, entity["id"], entity["version"], pending,
            {**entity["data"], **patch},
        )
        self.repository.append_audit_conn(
            connection, entity["id"], actor.user_id, actor.role,
            "invalidate", entity["status"], pending,
            {"reason": "telemetry_revised", "telemetry_id": telemetry["id"],
             "previous_basis_revision": entity["data"].get("basis_revision"),
             "revision": new_revision},
        )

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
