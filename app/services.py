"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .models import SignSession, Signature
from .repository import (
    get_freeze,
    get_plan,
    get_sign_session,
    get_signature,
    insert_events,
    insert_freeze,
    insert_sign_session,
    insert_signature,
    list_signatures,
    load_events,
    load_events_up_to,
    max_event_id,
    save_sign_session,
    save_signature,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
    }


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


# ---------------------------------------------------------------------------
# 冻结签署会话
#
# 重要培养方案的学期冻结先由发起人生成签署会话，快照内容在发起时固定；
# 教务、学院、审计三方代表随后签署。人员变更或利益冲突只影响有效票，
# 不改变快照内容；达到法定人数后才能正式发布，发布前签名可撤回。
# ---------------------------------------------------------------------------

REQUIRED_ROLES: tuple[str, ...] = ("academic_affairs", "college", "audit")


class SignSessionValidationError(Exception):
    """会话参数不合法（缺角色、法定人数越界、利益冲突等）。"""


class SignSessionNotFoundError(Exception):
    pass


class SignSessionClosedError(Exception):
    """会话已过期或已发布，拒绝进一步变更。"""


class RoleNotFoundError(Exception):
    pass


class NotDelegateError(Exception):
    """签署人不是该角色当前代表。"""


class DelegateConflictError(Exception):
    """同一人不得同时担任多个角色代表（利益冲突）。"""


class SignatureNotFoundError(Exception):
    pass


class QuorumNotMetError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """SQLite 读出的时间可能是 naive，一律按 UTC 归一化。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _session_status(session: SignSession, now: datetime) -> str:
    if session.published_at is not None:
        return "published"
    if session.expires_at is not None and now >= _as_utc(session.expires_at):
        return "expired"
    return "open"


def _require_open_session(session: SignSession, now: datetime) -> None:
    status = _session_status(session, now)
    if status != "open":
        raise SignSessionClosedError(
            f"sign session '{session.session_id}' is {status}; "
            "no further changes allowed"
        )


def _valid_roles(session: SignSession, signatures: list[Signature]) -> set[str]:
    """有效票 = 未撤回且签署人仍是该角色当前代表的签名所覆盖的角色。"""
    current = dict(session.delegates)
    valid: set[str] = set()
    for sig in signatures:
        if sig.withdrawn_at is not None:
            continue
        if current.get(sig.role) == sig.signer_id:
            valid.add(sig.role)
    return valid


def _signature_view(
    session: SignSession, sig: Signature
) -> dict[str, Any]:
    current = dict(session.delegates)
    valid = sig.withdrawn_at is None and current.get(sig.role) == sig.signer_id
    return {
        "role": sig.role,
        "signer_id": sig.signer_id,
        "signed_at": _iso(sig.signed_at),
        "withdrawn_at": _iso(sig.withdrawn_at),
        "valid": valid,
    }


def _session_view(
    session: SignSession, signatures: list[Signature], now: datetime
) -> dict[str, Any]:
    valid = _valid_roles(session, signatures)
    return {
        "plan_version": session.plan_version,
        "session_id": session.session_id,
        "freeze_id": session.freeze_id,
        "status": _session_status(session, now),
        "quorum": session.quorum,
        "valid_votes": len(valid),
        "quorum_met": len(valid) >= session.quorum,
        "delegates": dict(session.delegates),
        "delegate_history": list(session.delegate_history),
        "signatures": [_signature_view(session, s) for s in signatures],
        "event_cutoff_id": session.event_cutoff_id,
        "snapshot": session.snapshot,
        "created_by": session.created_by,
        "created_at": _iso(session.created_at),
        "expires_at": _iso(session.expires_at),
        "published_at": _iso(session.published_at),
    }


def _require_session(
    db: Session, plan_version: str, session_id: str
) -> SignSession:
    _require_plan(db, plan_version)
    session = get_sign_session(db, plan_version, session_id)
    if session is None:
        raise SignSessionNotFoundError(
            f"sign session '{session_id}' for plan '{plan_version}' does not exist"
        )
    return session


def _require_role(session: SignSession, role: str) -> None:
    if role not in dict(session.delegates):
        raise RoleNotFoundError(
            f"role '{role}' is not part of sign session '{session.session_id}'"
        )


def _validate_delegates(delegates: dict[str, str]) -> dict[str, str]:
    cleaned = {str(role).strip(): str(person).strip() for role, person in delegates.items()}
    missing = [role for role in REQUIRED_ROLES if role not in cleaned]
    extra = [role for role in cleaned if role not in REQUIRED_ROLES]
    if missing or extra:
        raise SignSessionValidationError(
            f"delegates must cover exactly {list(REQUIRED_ROLES)}; "
            f"missing={missing}, extra={extra}"
        )
    if any(not person for person in cleaned.values()):
        raise SignSessionValidationError("delegate identifiers must be non-empty")
    if len(set(cleaned.values())) != len(cleaned):
        raise SignSessionValidationError(
            "the same person cannot represent multiple roles (conflict of interest)"
        )
    return cleaned


def initiate_sign_session(
    db: Session,
    *,
    plan_version: str,
    session_id: str,
    freeze_id: str,
    delegates: dict[str, str],
    quorum: int | None = None,
    ttl_seconds: int | None = None,
    expires_at: datetime | None = None,
    created_by: str = "",
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """发起签署会话：此时固定快照内容，之后签署不改变内容。"""
    plan = _require_plan(db, plan_version)
    instant = _as_utc(now) if now is not None else _utcnow()

    existing = get_sign_session(db, plan_version, session_id)
    if existing is not None:
        signatures = list_signatures(db, plan_version, session_id)
        return _session_view(existing, signatures, instant), False

    cleaned = _validate_delegates(delegates)
    if quorum is None:
        quorum = len(REQUIRED_ROLES)
    if not 1 <= quorum <= len(REQUIRED_ROLES):
        raise SignSessionValidationError(
            f"quorum must be between 1 and {len(REQUIRED_ROLES)}"
        )
    if ttl_seconds is not None and expires_at is not None:
        raise SignSessionValidationError("pass either ttl_seconds or expires_at, not both")
    if ttl_seconds is not None:
        if ttl_seconds <= 0:
            raise SignSessionValidationError("ttl_seconds must be positive")
        expiry: datetime | None = instant + timedelta(seconds=ttl_seconds)
    elif expires_at is not None:
        expiry = _as_utc(expires_at)
    else:
        expiry = None

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
        generated_at=instant,
    )
    session = insert_sign_session(
        db,
        plan_version=plan_version,
        session_id=session_id,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
        quorum=quorum,
        delegates=cleaned,
        created_by=created_by.strip(),
        expires_at=expiry,
    )
    if session is None:
        # 并发发起：只有一个成功，其余读取既有会话。
        existing = get_sign_session(db, plan_version, session_id)
        assert existing is not None
        signatures = list_signatures(db, plan_version, session_id)
        return _session_view(existing, signatures, instant), False
    return _session_view(session, [], instant), True


def get_sign_session_view(
    db: Session, plan_version: str, session_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    session = _require_session(db, plan_version, session_id)
    instant = _as_utc(now) if now is not None else _utcnow()
    signatures = list_signatures(db, plan_version, session_id)
    return _session_view(session, signatures, instant)


def sign_session(
    db: Session,
    plan_version: str,
    session_id: str,
    role: str,
    signer_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """角色当前代表签署；重复签署幂等，撤回后重签会重新生效。"""
    session = _require_session(db, plan_version, session_id)
    instant = _as_utc(now) if now is not None else _utcnow()
    _require_open_session(session, instant)
    _require_role(session, role)

    signer = signer_id.strip()
    if not signer:
        raise SignSessionValidationError("signer_id must be non-empty")
    if dict(session.delegates).get(role) != signer:
        raise NotDelegateError(
            f"'{signer}' is not the current delegate for role '{role}'"
        )

    created = insert_signature(
        db,
        plan_version=plan_version,
        session_id=session_id,
        role=role,
        signer_id=signer,
        signed_at=instant,
    )
    if created is None:
        # 已存在同一代表同一角色的签名：若已撤回则重新生效，否则幂等。
        existing = get_signature(db, plan_version, session_id, role, signer)
        assert existing is not None
        if existing.withdrawn_at is not None:
            existing.withdrawn_at = None
            existing.signed_at = instant
            save_signature(db, existing)

    session = _require_session(db, plan_version, session_id)
    signatures = list_signatures(db, plan_version, session_id)
    return _session_view(session, signatures, instant)


def withdraw_signature(
    db: Session,
    plan_version: str,
    session_id: str,
    role: str,
    signer_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """撤回签名；只在发布前（且会话未过期）有效。"""
    session = _require_session(db, plan_version, session_id)
    instant = _as_utc(now) if now is not None else _utcnow()
    _require_open_session(session, instant)
    _require_role(session, role)

    signer = signer_id.strip()
    signature = get_signature(db, plan_version, session_id, role, signer)
    if signature is None or signature.withdrawn_at is not None:
        raise SignatureNotFoundError(
            f"no active signature by '{signer}' for role '{role}'"
        )
    signature.withdrawn_at = instant
    save_signature(db, signature)

    session = _require_session(db, plan_version, session_id)
    signatures = list_signatures(db, plan_version, session_id)
    return _session_view(session, signatures, instant)


def replace_delegate(
    db: Session,
    plan_version: str,
    session_id: str,
    role: str,
    new_delegate: str,
    *,
    reason: str = "",
    actor_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """替换角色代表：有效票随之变化，但快照内容保持不变。"""
    session = _require_session(db, plan_version, session_id)
    instant = _as_utc(now) if now is not None else _utcnow()
    _require_open_session(session, instant)
    _require_role(session, role)

    replacement = new_delegate.strip()
    if not replacement:
        raise SignSessionValidationError("delegate_id must be non-empty")

    current = dict(session.delegates)
    previous = current[role]
    if previous == replacement:
        signatures = list_signatures(db, plan_version, session_id)
        return _session_view(session, signatures, instant)
    if replacement in current.values():
        raise DelegateConflictError(
            f"'{replacement}' already represents another role in this session"
        )

    current[role] = replacement
    session.delegates = current
    history = list(session.delegate_history)
    history.append(
        {
            "role": role,
            "previous_delegate": previous,
            "new_delegate": replacement,
            "reason": reason.strip(),
            "actor_id": actor_id.strip(),
            "replaced_at": _iso(instant),
        }
    )
    session.delegate_history = history
    save_sign_session(db, session)

    signatures = list_signatures(db, plan_version, session_id)
    return _session_view(session, signatures, instant)


def publish_sign_session(
    db: Session,
    plan_version: str,
    session_id: str,
    *,
    now: datetime | None = None,
) -> Snapshot:
    """达到法定人数后正式发布：以会话固定的快照内容落库为冻结。"""
    session = _require_session(db, plan_version, session_id)
    instant = _as_utc(now) if now is not None else _utcnow()

    if session.published_at is not None:
        # 幂等：已发布的会话返回既有冻结内容。
        return get_frozen_snapshot(db, plan_version, session.freeze_id)
    _require_open_session(session, instant)

    signatures = list_signatures(db, plan_version, session_id)
    valid = _valid_roles(session, signatures)
    if len(valid) < session.quorum:
        raise QuorumNotMetError(
            f"sign session '{session_id}' has {len(valid)} valid vote(s), "
            f"quorum is {session.quorum}"
        )

    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=session.freeze_id,
        snapshot=dict(session.snapshot),
        event_cutoff_id=session.event_cutoff_id,
    )
    session.published_at = instant
    save_sign_session(db, session)
    if row is None:
        return get_frozen_snapshot(db, plan_version, session.freeze_id)
    return Snapshot.from_dict(row.snapshot)
