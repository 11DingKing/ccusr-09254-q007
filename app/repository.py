"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import Freeze, Plan, SignDelegate, Signature, SignSession


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


def get_sign_session(
    db: Session, plan_version: str, session_id: str
) -> SignSession | None:
    return db.get(SignSession, (plan_version, session_id))


def insert_sign_session(
    db: Session,
    *,
    plan_version: str,
    session_id: str,
    freeze_id: str,
    content_hash: str,
    quorum: int,
    initiator: str,
    expires_at: datetime | None,
    delegates: dict[str, str],
) -> SignSession | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(SignSession).values(
        plan_version=plan_version,
        session_id=session_id,
        freeze_id=freeze_id,
        content_hash=content_hash,
        quorum=quorum,
        initiator=initiator,
        expires_at=expires_at,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "session_id"]
    ).returning(SignSession.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    if inserted is not None:
        for role in sorted(delegates):
            delegate_stmt = sqlite_insert(SignDelegate).values(
                plan_version=plan_version,
                session_id=session_id,
                role=role,
                delegate_id=delegates[role],
                conflict_of_interest=False,
            )
            db.execute(
                delegate_stmt.on_conflict_do_nothing(
                    index_elements=["plan_version", "session_id", "role"]
                )
            )
    db.commit()
    if inserted is not None:
        return db.get(SignSession, (plan_version, session_id))
    return None


def get_delegate(
    db: Session, plan_version: str, session_id: str, role: str
) -> SignDelegate | None:
    return db.get(SignDelegate, (plan_version, session_id, role))


def get_delegates(
    db: Session, plan_version: str, session_id: str
) -> list[SignDelegate]:
    stmt = (
        select(SignDelegate)
        .where(SignDelegate.plan_version == plan_version)
        .where(SignDelegate.session_id == session_id)
    )
    return list(db.execute(stmt).scalars().all())


def replace_delegate(
    db: Session,
    *,
    plan_version: str,
    session_id: str,
    role: str,
    delegate_id: str,
    conflict_of_interest: bool,
) -> SignDelegate:
    """替换代表：旧代表的有效签名被取代，新代表上任。"""
    now = datetime.now(timezone.utc)
    supersede_stmt = (
        update(Signature)
        .where(Signature.plan_version == plan_version)
        .where(Signature.session_id == session_id)
        .where(Signature.role == role)
        .where(Signature.signer_id != delegate_id)
        .where(Signature.status == "active")
        .values(status="superseded", updated_at=now)
    )
    db.execute(supersede_stmt)
    stmt = sqlite_insert(SignDelegate).values(
        plan_version=plan_version,
        session_id=session_id,
        role=role,
        delegate_id=delegate_id,
        conflict_of_interest=conflict_of_interest,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version", "session_id", "role"],
        set_={
            "delegate_id": delegate_id,
            "conflict_of_interest": conflict_of_interest,
            "updated_at": now,
        },
    )
    db.execute(stmt)
    db.commit()
    row = get_delegate(db, plan_version, session_id, role)
    assert row is not None
    return row


def get_signature(
    db: Session, plan_version: str, session_id: str, role: str, signer_id: str
) -> Signature | None:
    return db.get(Signature, (plan_version, session_id, role, signer_id))


def get_signatures(
    db: Session, plan_version: str, session_id: str
) -> list[Signature]:
    stmt = (
        select(Signature)
        .where(Signature.plan_version == plan_version)
        .where(Signature.session_id == session_id)
    )
    return list(db.execute(stmt).scalars().all())


def insert_signature(
    db: Session,
    *,
    plan_version: str,
    session_id: str,
    role: str,
    signer_id: str,
    content_hash: str,
) -> Signature | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Signature).values(
        plan_version=plan_version,
        session_id=session_id,
        role=role,
        signer_id=signer_id,
        content_hash=content_hash,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "session_id", "role", "signer_id"]
    ).returning(Signature.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Signature, (plan_version, session_id, role, signer_id))
    return None


def update_signature_status(
    db: Session,
    signature: Signature,
    *,
    status: str,
    content_hash: str | None = None,
    signed_at: datetime | None = None,
) -> Signature:
    """执行确定性的业务处理。"""
    signature.status = status
    signature.updated_at = datetime.now(timezone.utc)
    if content_hash is not None:
        signature.content_hash = content_hash
    if signed_at is not None:
        signature.signed_at = signed_at
    db.commit()
    return signature


def mark_session_published(
    db: Session, plan_version: str, session_id: str, published_at: datetime
) -> bool:
    """条件更新保证并发发布时只有一个事务完成状态迁移。"""
    stmt = (
        update(SignSession)
        .where(SignSession.plan_version == plan_version)
        .where(SignSession.session_id == session_id)
        .where(SignSession.status == "open")
        .values(status="published", published_at=published_at)
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount == 1
