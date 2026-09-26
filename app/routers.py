"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    DelegateReplaceIn,
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    SignIn,
    SignSessionInitIn,
    SignSessionOut,
    SnapshotOut,
    StudentProgressOut,
    WithdrawIn,
)

router = APIRouter(prefix="/api")


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _sign_session_http_errors(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        (
            services.PlanNotFoundError,
            services.FreezeNotFoundError,
            services.SignSessionNotFoundError,
            services.SignatureNotFoundError,
        ),
    ):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, services.NotDelegateError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(
        exc,
        (
            services.SignSessionClosedError,
            services.QuorumNotMetError,
            services.SnapshotContentChangedError,
        ),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, services.SignSessionConfigError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}/sign-sessions/{session_id}",
    response_model=SignSessionOut,
    status_code=status.HTTP_201_CREATED,
)
def initiate_sign_session(
    plan_version: str,
    freeze_id: str,
    session_id: str,
    body: SignSessionInitIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        state, _ = services.initiate_sign_session(
            db,
            plan_version=plan_version,
            freeze_id=freeze_id,
            session_id=session_id,
            initiator=body.initiator,
            delegates=body.delegates,
            quorum=body.quorum,
            expires_at=body.expires_at,
        )
        return state
    except Exception as exc:
        raise _sign_session_http_errors(exc) from exc


@router.get(
    "/plans/{plan_version}/sign-sessions/{session_id}",
    response_model=SignSessionOut,
)
def get_sign_session(
    plan_version: str, session_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.get_sign_session_state(db, plan_version, session_id)
    except Exception as exc:
        raise _sign_session_http_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/sign-sessions/{session_id}/sign",
    response_model=SignSessionOut,
    status_code=status.HTTP_201_CREATED,
)
def sign_session(
    plan_version: str,
    session_id: str,
    body: SignIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.sign_session(
            db,
            plan_version=plan_version,
            session_id=session_id,
            role=body.role,
            signer_id=body.signer_id,
        )
    except Exception as exc:
        raise _sign_session_http_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/sign-sessions/{session_id}/withdraw",
    response_model=SignSessionOut,
)
def withdraw_signature(
    plan_version: str,
    session_id: str,
    body: WithdrawIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.withdraw_signature(
            db,
            plan_version=plan_version,
            session_id=session_id,
            role=body.role,
            signer_id=body.signer_id,
        )
    except Exception as exc:
        raise _sign_session_http_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/sign-sessions/{session_id}/delegates/{role}",
    response_model=SignSessionOut,
)
def replace_delegate(
    plan_version: str,
    session_id: str,
    role: str,
    body: DelegateReplaceIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.replace_delegate(
            db,
            plan_version=plan_version,
            session_id=session_id,
            role=role,
            delegate_id=body.delegate_id,
            conflict_of_interest=body.conflict_of_interest,
        )
    except Exception as exc:
        raise _sign_session_http_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/sign-sessions/{session_id}/publish",
    response_model=SignSessionOut,
)
def publish_sign_session(
    plan_version: str, session_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.publish_sign_session(
            db, plan_version=plan_version, session_id=session_id
        )
    except Exception as exc:
        raise _sign_session_http_errors(exc) from exc
