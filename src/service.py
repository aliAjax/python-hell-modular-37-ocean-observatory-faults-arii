import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


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
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        # Idempotent retry: an already-proposed plan is returned as-is.
        if entity["kind"] == "recovery_action" and action == "retry" and entity["status"] == "proposed":
            return entity
        # Manual recalculation does not change the telemetry itself.
        if entity["kind"] == "telemetry" and action == "recalculate":
            allowed = self.rules.ROLE_ACTIONS.get("recalculate", ("admin", "engineer"))
            if "*" not in allowed and actor.role not in allowed:
                raise PermissionDenied("role %s is not allowed here" % actor.role)
            self.recalculate(actor, entity)
            return entity
        expected = int(expected_version) if expected_version is not None else entity["version"]
        # Optimistic lock: a stale expected version is a revision conflict,
        # raised before any state or role validation.
        if expected_version is not None and int(entity["version"]) != expected:
            raise ConflictError(
                "version conflict: expected %s, found %s" % (expected, entity["version"])
            )
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] == "telemetry" and action == "revise":
            self.recalculate(actor, updated)
        return updated

    def recalculate(self, actor, telemetry):
        """Recalculate incident severity, invalidate dependent recovery plans,
        and recalculate linked missions after a telemetry revision."""
        asset_id = telemetry["data"].get("asset_id")
        new_revision = int(telemetry["data"].get("revision", 0))
        result = {"incidents": [], "recovery_actions": [], "missions": []}

        # 1. Recalculate incident severity from the revised telemetry.
        if asset_id:
            for incident in self.repository.list_entities("incident"):
                if incident["data"].get("asset_id") != asset_id:
                    continue
                if incident["status"] in ("resolved", "closed"):
                    continue
                new_severity = self.rules.recalculate_severity(telemetry)
                if new_severity != incident["data"].get("severity"):
                    merged = dict(incident["data"])
                    merged["severity"] = new_severity
                    updated = self.repository.update_entity(
                        incident["id"], incident["version"], incident["status"], merged
                    )
                    self.audit.record(
                        incident["id"], actor, "recalculate", incident["status"], updated["status"],
                        {"severity": new_severity, "telemetry_revision": new_revision},
                    )
                    result["incidents"].append(updated)

        # 2. Invalidate unexecuted recovery plans that depend on a stale revision.
        dependent_actions = []
        for action in self.repository.list_entities("recovery_action"):
            if action["data"].get("telemetry_id") != telemetry["id"]:
                continue
            if int(action["data"].get("telemetry_revision", 0)) >= new_revision:
                continue
            dependent_actions.append(action)
            if action["status"] in ("proposed", "approved"):
                updated = self.repository.update_entity(
                    action["id"], action["version"], "pending_confirmation", action["data"]
                )
                self.audit.record(
                    action["id"], actor, "recalculate", action["status"], "pending_confirmation",
                    {"reason": "telemetry revised", "telemetry_revision": new_revision},
                )
                result["recovery_actions"].append(updated)
            # Executed actions (running/succeeded/failed/cancelled) are kept as archive.

        # 3. Recalculate missions linked to invalidated plans.
        action_ids = {a["id"] for a in dependent_actions}
        for mission in self.repository.list_entities("mission"):
            if mission["data"].get("recovery_action_id") not in action_ids:
                continue
            if mission["status"] in ("planned", "approved"):
                updated = self.repository.update_entity(
                    mission["id"], mission["version"], "planned", mission["data"]
                )
                self.audit.record(
                    mission["id"], actor, "recalculate", mission["status"], "planned",
                    {"reason": "plan invalidated"},
                )
                result["missions"].append(updated)

        return result

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
