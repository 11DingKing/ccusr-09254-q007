"""服务端业务模块。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from sqlalchemy.orm import Session

from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .models import SignSession
from .repository import (
    get_delegate,
    get_delegates,
    get_freeze,
    get_plan,
    get_sign_session,
    get_signature,
    get_signatures,
    insert_events,
    insert_freeze,
    insert_sign_session,
    insert_signature,
    load_events,
    load_events_up_to,
    mark_session_published,
    max_event_id,
    replace_delegate as repo_replace_delegate,
    update_signature_status,
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
# 签署会话：重要培养方案的冻结快照需教务、学院、审计三方签署后方可正式发布。
# 快照内容在发起会话时以 content_hash 固定；人员变更或利益冲突只影响有效票，
# 不改变快照内容；达到法定人数（quorum）才允许发布，撤回仅在发布前有效。
# ---------------------------------------------------------------------------

SIGN_ROLES: tuple[str, ...] = ("registrar", "college", "audit")


class SignSessionNotFoundError(Exception):
    pass


class SignatureNotFoundError(Exception):
    pass


class SignSessionClosedError(Exception):
    """会话已发布或已过期，禁止再变更。"""


class QuorumNotMetError(Exception):
    pass


class SnapshotContentChangedError(Exception):
    pass


class NotDelegateError(Exception):
    """签署人不是该角色当前代表。"""


class SignSessionConfigError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _content_hash(snapshot: dict[str, Any]) -> str:
    raw = json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return sha256(raw).hexdigest()


def _session_expired(session: SignSession, now: datetime) -> bool:
    if session.expires_at is None:
        return False
    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return now >= expires


def _require_sign_session(
    db: Session, plan_version: str, session_id: str
) -> SignSession:
    session = get_sign_session(db, plan_version, session_id)
    if session is None:
        raise SignSessionNotFoundError(
            f"sign session '{session_id}' for plan '{plan_version}' does not exist"
        )
    return session


def _require_session_open(session: SignSession) -> None:
    if session.status == "published":
        raise SignSessionClosedError(
            f"sign session '{session.session_id}' is already published"
        )
    if _session_expired(session, _utcnow()):
        raise SignSessionClosedError(
            f"sign session '{session.session_id}' has expired"
        )


def _require_role(role: str) -> None:
    if role not in SIGN_ROLES:
        raise SignSessionConfigError(
            f"unknown role '{role}'; expected one of {', '.join(SIGN_ROLES)}"
        )


def _session_state(db: Session, session: SignSession) -> dict[str, Any]:
    delegates = get_delegates(db, session.plan_version, session.session_id)
    signatures = get_signatures(db, session.plan_version, session.session_id)
    delegate_by_role = {d.role: d for d in delegates}

    def _counts(sig) -> bool:
        delegate = delegate_by_role.get(sig.role)
        return (
            sig.status == "active"
            and delegate is not None
            and delegate.delegate_id == sig.signer_id
            and not delegate.conflict_of_interest
        )

    valid_votes = sum(1 for sig in signatures if _counts(sig))
    return {
        "plan_version": session.plan_version,
        "session_id": session.session_id,
        "freeze_id": session.freeze_id,
        "status": session.status,
        "expired": _session_expired(session, _utcnow()),
        "quorum": session.quorum,
        "valid_votes": valid_votes,
        "content_hash": session.content_hash,
        "initiator": session.initiator,
        "created_at": _iso(session.created_at),
        "expires_at": _iso(session.expires_at),
        "published_at": _iso(session.published_at),
        "delegates": [
            {
                "role": d.role,
                "delegate_id": d.delegate_id,
                "conflict_of_interest": d.conflict_of_interest,
            }
            for d in sorted(delegates, key=lambda item: item.role)
        ],
        "signatures": [
            {
                "role": s.role,
                "signer_id": s.signer_id,
                "status": s.status,
                "content_hash": s.content_hash,
                "signed_at": _iso(s.signed_at),
                "counts_toward_quorum": _counts(s),
            }
            for s in sorted(signatures, key=lambda item: (item.role, item.signer_id))
        ],
    }


def _validate_delegates(delegates: dict[str, str], quorum: int | None) -> None:
    if set(delegates) != set(SIGN_ROLES):
        raise SignSessionConfigError(
            "delegates must name exactly one representative per role: "
            + ", ".join(SIGN_ROLES)
        )
    if any(not str(value).strip() for value in delegates.values()):
        raise SignSessionConfigError("delegate ids must be non-empty")
    if quorum is not None and not 1 <= quorum <= len(delegates):
        raise SignSessionConfigError(
            f"quorum must be between 1 and {len(delegates)}"
        )


def initiate_sign_session(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    session_id: str,
    initiator: str,
    delegates: dict[str, str],
    quorum: int | None = None,
    expires_at: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """发起签署会话：快照内容在此刻以 content_hash 固定。"""
    _require_plan(db, plan_version)
    freeze = get_freeze(db, plan_version, freeze_id)
    if freeze is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    _validate_delegates(delegates, quorum)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            raise SignSessionConfigError("expires_at must be timezone-aware")
        expires_at = expires_at.astimezone(timezone.utc)
    row = insert_sign_session(
        db,
        plan_version=plan_version,
        session_id=session_id,
        freeze_id=freeze_id,
        content_hash=_content_hash(freeze.snapshot),
        quorum=quorum if quorum is not None else len(delegates),
        initiator=initiator,
        expires_at=expires_at,
        delegates=delegates,
    )
    if row is None:
        existing = _require_sign_session(db, plan_version, session_id)
        return _session_state(db, existing), False
    return _session_state(db, row), True


def get_sign_session_state(
    db: Session, plan_version: str, session_id: str
) -> dict[str, Any]:
    session = _require_sign_session(db, plan_version, session_id)
    return _session_state(db, session)


def sign_session(
    db: Session, *, plan_version: str, session_id: str, role: str, signer_id: str
) -> dict[str, Any]:
    """签署：仅该角色当前代表可签；重复签署幂等，不会重复计票。"""
    session = _require_sign_session(db, plan_version, session_id)
    _require_session_open(session)
    _require_role(role)
    delegate = get_delegate(db, plan_version, session_id, role)
    if delegate is None or delegate.delegate_id != signer_id:
        raise NotDelegateError(
            f"'{signer_id}' is not the current delegate for role '{role}'"
        )
    existing = get_signature(db, plan_version, session_id, role, signer_id)
    if existing is not None:
        if existing.status != "active":
            update_signature_status(
                db,
                existing,
                status="active",
                content_hash=session.content_hash,
                signed_at=_utcnow(),
            )
        return _session_state(db, session)
    # 并发签署由唯一约束兜底：同人同角色只落一行，其余请求幂等返回。
    insert_signature(
        db,
        plan_version=plan_version,
        session_id=session_id,
        role=role,
        signer_id=signer_id,
        content_hash=session.content_hash,
    )
    return _session_state(db, session)


def withdraw_signature(
    db: Session, *, plan_version: str, session_id: str, role: str, signer_id: str
) -> dict[str, Any]:
    """撤回签名：仅在发布前有效。"""
    session = _require_sign_session(db, plan_version, session_id)
    _require_session_open(session)
    _require_role(role)
    signature = get_signature(db, plan_version, session_id, role, signer_id)
    if signature is None or signature.status != "active":
        raise SignatureNotFoundError(
            f"no active signature from '{signer_id}' for role '{role}'"
        )
    update_signature_status(db, signature, status="withdrawn")
    return _session_state(db, session)


def replace_delegate(
    db: Session,
    *,
    plan_version: str,
    session_id: str,
    role: str,
    delegate_id: str,
    conflict_of_interest: bool = False,
) -> dict[str, Any]:
    """替换代表或标记利益冲突：影响有效票，但不改变快照内容。"""
    session = _require_sign_session(db, plan_version, session_id)
    _require_session_open(session)
    _require_role(role)
    if not delegate_id.strip():
        raise SignSessionConfigError("delegate id must be non-empty")
    repo_replace_delegate(
        db,
        plan_version=plan_version,
        session_id=session_id,
        role=role,
        delegate_id=delegate_id,
        conflict_of_interest=conflict_of_interest,
    )
    return _session_state(db, session)


def publish_sign_session(
    db: Session, *, plan_version: str, session_id: str
) -> dict[str, Any]:
    """发布：达到法定人数才允许；状态迁移原子完成，重复发布幂等。"""
    session = _require_sign_session(db, plan_version, session_id)
    if session.status == "published":
        return _session_state(db, session)
    if _session_expired(session, _utcnow()):
        raise SignSessionClosedError(
            f"sign session '{session.session_id}' has expired"
        )
    freeze = get_freeze(db, plan_version, session.freeze_id)
    if freeze is None or _content_hash(freeze.snapshot) != session.content_hash:
        raise SnapshotContentChangedError(
            "frozen snapshot content changed since the session was initiated"
        )
    state = _session_state(db, session)
    if state["valid_votes"] < session.quorum:
        raise QuorumNotMetError(
            f"valid votes {state['valid_votes']} below quorum {session.quorum}"
        )
    mark_session_published(db, plan_version, session_id, _utcnow())
    session = _require_sign_session(db, plan_version, session_id)
    return _session_state(db, session)
