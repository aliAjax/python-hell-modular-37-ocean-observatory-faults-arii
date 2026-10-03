from datetime import datetime, timedelta

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


ACTIVE_INCIDENT_STATUSES = ("open", "diagnosing", "recovery_planned", "recovering")
TERMINAL_ACTION_STATUSES = ("succeeded", "failed", "cancelled")

SEVERITY_ORDER = ("low", "medium", "high", "critical")
# 原型阈值：遥测值越高故障越严重，用于迟到修订后的严重度重算。
_SEVERITY_BANDS = (("critical", 85.0), ("high", 70.0), ("medium", 40.0))


def recalculate_severity(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError("telemetry value must be numeric to derive severity")
    for level, bound in _SEVERITY_BANDS:
        if number >= bound:
            return level
    return "low"


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _latest_telemetry(lookup, asset_id, metric):
    """Latest-revision telemetry point for an asset/metric (late revisions included)."""
    matches = [
        item
        for item in _all(lookup, "telemetry")
        if item["data"].get("asset_id") == asset_id and item["data"].get("metric") == metric
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: int(item["data"].get("revision", 0)))


def _resolve_basis_telemetry(data, lookup):
    """Resolve the telemetry point an incident/plan is based on.

    Explicit basis_telemetry_id wins; otherwise the latest revision of the
    asset/metric pair is used. Returns None when no telemetry is referenced.
    """
    explicit = data.get("basis_telemetry_id")
    if explicit:
        telemetry = _find_one(lookup, "telemetry", "id", explicit)
        if not telemetry:
            raise ValidationError("basis_telemetry_id does not exist")
        return telemetry
    asset_id = data.get("asset_id")
    metric = data.get("metric")
    if asset_id and metric:
        return _latest_telemetry(lookup, asset_id, metric)
    return None


def _number(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")
    if number < 0:
        raise ValidationError(field + " must be non-negative")
    return number


def _validate_asset(data, lookup):
    if not _find_one(lookup, "station", "id", data.get("station_id")):
        raise ValidationError("asset requires station")
    if data.get("clock_offset_seconds") not in (None, ""):
        _number(data.get("clock_offset_seconds"), "clock_offset_seconds")


def _validate_link(data, lookup):
    if not _find_one(lookup, "station", "id", data.get("station_id")):
        raise ValidationError("link requires station")
    if not _find_one(lookup, "asset", "id", data.get("asset_id")):
        raise ValidationError("link requires asset")
    _number(data.get("capacity"), "capacity")


def _validate_telemetry(data, lookup):
    asset = _find_one(lookup, "asset", "id", data.get("asset_id"))
    if not asset:
        raise ValidationError("telemetry requires asset")
    _number(data.get("value"), "value")
    try:
        revision = int(data.get("revision"))
    except (TypeError, ValueError):
        raise ValidationError("revision must be an integer")
    if revision < 1:
        raise ValidationError("revision must be positive")
    for item in _all(lookup, "telemetry"):
        if item["data"].get("asset_id") == data.get("asset_id") and item["data"].get("metric") == data.get("metric"):
            if int(item["data"].get("revision", 0)) >= revision:
                raise ConflictError("telemetry revision must increase")


def _validate_incident(data, lookup):
    if not data.get("station_id") and not data.get("asset_id") and not data.get("link_id"):
        raise ValidationError("incident requires station_id, asset_id or link_id")
    basis = _resolve_basis_telemetry(data, lookup)
    severity = data.get("severity")
    if severity not in (None, ""):
        if severity not in SEVERITY_ORDER:
            raise ValidationError("invalid incident severity")
    elif basis:
        # 创建时未显式给定严重度时，按所依据的遥测修订重算。
        severity = recalculate_severity(basis["data"].get("value"))
        data["severity"] = severity
    else:
        raise ValidationError("incident severity or a telemetry basis is required")
    if basis:
        data["basis_telemetry_id"] = basis["id"]
        data["basis_revision"] = int(basis["data"].get("revision", 0))
    for item in _all(lookup, "incident"):
        if (
            item["status"] in ACTIVE_INCIDENT_STATUSES
            and item["data"].get("asset_id") == data.get("asset_id")
            and item["data"].get("kind") == data.get("kind")
            and data.get("asset_id")
        ):
            raise ConflictError("active incident already exists for asset and kind")


def _validate_action(data, lookup):
    incident = _find_one(lookup, "incident", "id", data.get("incident_id"))
    if not incident or incident["status"] in ("resolved", "closed"):
        raise ValidationError("recovery action requires an active incident")
    if data.get("action_type") not in ("remote_restart", "switch_backup", "firmware_rollback", "dispatch_mission"):
        raise ValidationError("invalid action_type")
    if not data.get("dedupe_key"):
        raise ValidationError("missing required field: dedupe_key")
    # 恢复方案生成时记下采用的遥测修订；未完成对象重试时沿用，不重新解析。
    basis_id = incident["data"].get("basis_telemetry_id")
    if basis_id:
        data.setdefault("basis_telemetry_id", basis_id)
        data.setdefault("basis_revision", int(incident["data"].get("basis_revision", 0)))
    # 活动去重冲突由服务层（并发）和数据库唯一索引（竞态兜底）处理。


def _validate_mission(data, lookup):
    if not _find_one(lookup, "station", "id", data.get("station_id")):
        raise ValidationError("mission requires station")
    if not data.get("window_start") or not data.get("window_end"):
        raise ValidationError("mission window is required")
    if data.get("incident_id"):
        incident = _find_one(lookup, "incident", "id", data.get("incident_id"))
        if not incident:
            raise ValidationError("mission incident_id does not exist")
        basis_id = incident["data"].get("basis_telemetry_id")
        if basis_id:
            data.setdefault("basis_telemetry_id", basis_id)
            data.setdefault("basis_revision", int(incident["data"].get("basis_revision", 0)))


def _validate_gap(data, lookup):
    if not _find_one(lookup, "incident", "id", data.get("incident_id")):
        raise ValidationError("data gap requires incident")
    if not data.get("start_at") or not data.get("end_at"):
        raise ValidationError("gap window is required")


def _revise_telemetry(actor, entity, data, lookup):
    try:
        new_revision = int(data.get("revision"))
    except (TypeError, ValueError):
        raise ValidationError("revision must be an integer")
    if new_revision <= int(entity["data"].get("revision", 0)):
        raise ConflictError(
            "telemetry revision conflict: late revision %s is not higher than current %s"
            % (new_revision, entity["data"].get("revision", 0))
        )
    return {
        "late_revision": True,
        "revised_by": actor.user_id,
        "supersedes_revision": int(entity["data"].get("revision", 0)),
    }


def _guard_not_stale(actor, entity, data, lookup):
    if entity["data"].get("stale"):
        raise ConflictError(
            "%s is invalidated by telemetry revision %s; engineer must reconfirm first"
            % (entity["kind"], entity["data"].get("invalidated_by_revision"))
        )
    return {}


def _reconfirm_recovery_action(actor, entity, data, lookup):
    if not entity["data"].get("stale"):
        raise ConflictError("recovery action is not pending reconfirmation")
    basis_id = entity["data"].get("basis_telemetry_id")
    telemetry = _find_one(lookup, "telemetry", "id", basis_id) if basis_id else None
    if not telemetry:
        raise ConflictError("basis telemetry is unavailable for reconfirmation")
    return {
        "basis_revision": int(telemetry["data"].get("revision", 0)),
        "reconfirmed_by": actor.user_id,
        "__drop__": ["stale", "invalidated_by_revision"],
    }


def _reconfirm_mission(actor, entity, data, lookup):
    if not entity["data"].get("stale"):
        raise ConflictError("mission is not pending reconfirmation")
    basis_id = entity["data"].get("basis_telemetry_id")
    telemetry = _find_one(lookup, "telemetry", "id", basis_id) if basis_id else None
    if not telemetry:
        raise ConflictError("basis telemetry is unavailable for reconfirmation")
    return {
        "basis_revision": int(telemetry["data"].get("revision", 0)),
        "reconfirmed_by": actor.user_id,
        "__drop__": ["stale", "invalidated_by_revision"],
    }


def _resolve_incident(actor, entity, data, lookup):
    actions = [a for a in _all(lookup, "recovery_action") if a["data"].get("incident_id") == entity["id"] and a["status"] not in ("succeeded", "failed", "cancelled")]
    if actions:
        raise ConflictError("incident cannot resolve while recovery actions are active")
    gaps = [g for g in _all(lookup, "gap") if g["data"].get("incident_id") == entity["id"] and g["status"] not in ("filled", "accepted", "closed")]
    if gaps:
        raise ConflictError("incident cannot resolve while data gaps remain open")
    assets = [a for a in _all(lookup, "asset") if a["status"] in ("faulty", "offline", "rebooting")]
    if entity["data"].get("asset_id") and any(a["id"] == entity["data"].get("asset_id") for a in assets):
        raise ConflictError("affected asset is still unavailable")
    return {"resolved_by": actor.user_id}


def _complete_action(actor, entity, data, lookup):
    if not data.get("outcome"):
        raise ValidationError("outcome is required")
    return {"completed_by": actor.user_id}


def _complete_mission(actor, entity, data, lookup):
    if not data.get("report"):
        raise ValidationError("report is required")
    return {"completed_by": actor.user_id}


class RuleEngine:
    ALIASES = {
        "stations": "station", "assets": "asset", "links": "link", "telemetries": "telemetry",
        "incidents": "incident", "recovery_actions": "recovery_action", "missions": "mission",
        "gaps": "gap",
    }
    INITIAL_STATUS = {
        "station": "online", "asset": "healthy", "link": "up", "telemetry": "current",
        "incident": "open", "recovery_action": "proposed", "mission": "planned", "gap": "open",
    }
    TRANSITIONS = {
        "station": {
            "degrade": (("online",), "degraded"),
            "go_offline": (("online", "degraded"), "offline"),
            "resume": (("degraded", "offline"), "online"),
        },
        "asset": {
            "degrade": (("healthy",), "degraded"),
            "fail": (("healthy", "degraded"), "faulty"),
            "start_reboot": (("faulty",), "rebooting"),
            "finish_reboot": (("rebooting",), "healthy"),
            "restore": (("faulty",), "healthy"),
        },
        "link": {
            "degrade": (("up",), "degraded"),
            "fail": (("up", "degraded"), "down"),
            "activate_backup": (("down", "degraded"), "backup_active"),
            "restore": (("down", "backup_active", "degraded"), "up"),
        },
        "telemetry": {
            "mark_stale": (("current",), "stale"),
            "quarantine": (("current", "stale"), "quarantined"),
            "revise": (("current", "stale", "quarantined"), "current"),
            "clear": (("stale",), "current"),
        },
        "incident": {
            "diagnose": (("open",), "diagnosing"),
            "plan_recovery": (("diagnosing",), "recovery_planned"),
            "start_recovery": (("recovery_planned",), "recovering"),
            "resolve": (("recovering",), "resolved"),
            "close": (("resolved",), "closed"),
            "reopen": (("resolved", "closed"), "open"),
        },
        "recovery_action": {
            "approve": (("proposed",), "approved"),
            "reconfirm": (("proposed", "approved"), "proposed"),
            "start": (("approved",), "running"),
            "succeed": (("running",), "succeeded"),
            "fail": (("running",), "failed"),
            "cancel": (("proposed", "approved", "running"), "cancelled"),
        },
        "mission": {
            "approve": (("planned",), "approved"),
            "reconfirm": (("planned", "approved"), "planned"),
            "depart": (("approved",), "underway"),
            "complete": (("underway",), "completed"),
            "cancel": (("planned", "approved", "underway"), "cancelled"),
        },
        "gap": {
            "estimate": (("open",), "estimated"),
            "fill": (("estimated",), "filled"),
            "accept": (("filled", "open"), "accepted"),
        },
    }
    CREATE_REQUIRED = {
        "station": ("name", "region"),
        "asset": ("station_id", "asset_type", "serial_no", "last_seen"),
        "link": ("station_id", "asset_id", "link_type", "capacity"),
        "telemetry": ("asset_id", "metric", "value", "observed_at", "revision"),
        "incident": ("kind", "summary"),
        "recovery_action": ("incident_id", "action_type", "dedupe_key"),
        "mission": ("station_id", "purpose", "window_start", "window_end"),
        "gap": ("incident_id", "start_at", "end_at"),
    }
    ACTION_REQUIRED = {
        ("station", "degrade"): ("reason",),
        ("link", "fail"): ("reason",),
        ("telemetry", "revise"): ("revision",),
        ("recovery_action", "succeed"): ("outcome",),
        ("mission", "complete"): ("report",),
        ("gap", "fill"): ("estimate",),
        ("incident", "resolve"): ("summary",),
    }
    CREATE_ROLES = {
        "station": ("admin", "engineer"),
        "asset": ("admin", "engineer"),
        "link": ("admin", "engineer"),
        "telemetry": ("admin", "operator", "engineer"),
        "incident": ("admin", "operator", "engineer"),
        "recovery_action": ("admin", "operator", "engineer"),
        "mission": ("admin", "engineer"),
        "gap": ("admin", "operator", "engineer"),
    }
    ROLE_ACTIONS = {
        "degrade": ("admin", "engineer", "operator"),
        "go_offline": ("admin", "engineer", "operator"),
        "resume": ("admin", "engineer", "operator"),
        "fail": ("admin", "engineer", "operator"),
        "start_reboot": ("admin", "engineer", "operator"),
        "finish_reboot": ("admin", "engineer", "operator"),
        "restore": ("admin", "engineer", "operator"),
        "activate_backup": ("admin", "engineer", "operator"),
        "mark_stale": ("admin", "operator", "engineer"),
        "quarantine": ("admin", "engineer", "operator"),
        "revise": ("admin", "operator", "engineer"),
        "clear": ("admin", "operator", "engineer"),
        "diagnose": ("admin", "operator", "engineer"),
        "plan_recovery": ("admin", "operator", "engineer"),
        "start_recovery": ("admin", "operator", "engineer"),
        "resolve": ("admin", "engineer"),
        "close": ("admin", "engineer"),
        "reopen": ("admin", "engineer", "operator"),
        "approve": ("admin", "engineer"),
        "reconfirm": ("admin", "engineer"),
        "start": ("admin", "engineer", "operator"),
        "succeed": ("admin", "engineer", "operator"),
        "cancel": ("admin", "engineer", "operator"),
        "depart": ("admin", "engineer", "operator"),
        "estimate": ("admin", "engineer", "operator"),
        "fill": ("admin", "engineer", "operator"),
        "accept": ("admin", "engineer", "operator"),
    }
    CUSTOM_CREATE = {
        "asset": lambda a, d, l: _validate_asset(d, l),
        "link": lambda a, d, l: _validate_link(d, l),
        "telemetry": lambda a, d, l: _validate_telemetry(d, l),
        "incident": lambda a, d, l: _validate_incident(d, l),
        "recovery_action": lambda a, d, l: _validate_action(d, l),
        "mission": lambda a, d, l: _validate_mission(d, l),
        "gap": lambda a, d, l: _validate_gap(d, l),
    }
    CUSTOM_TRANSITIONS = {
        ("telemetry", "revise"): _revise_telemetry,
        ("incident", "resolve"): _resolve_incident,
        ("recovery_action", "succeed"): _complete_action,
        ("mission", "complete"): _complete_mission,
        ("recovery_action", "approve"): _guard_not_stale,
        ("mission", "approve"): _guard_not_stale,
        ("recovery_action", "start"): _guard_not_stale,
        ("mission", "depart"): _guard_not_stale,
        ("recovery_action", "reconfirm"): _reconfirm_recovery_action,
        ("mission", "reconfirm"): _reconfirm_mission,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        drop = extra.pop("__drop__", [])
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch, drop
