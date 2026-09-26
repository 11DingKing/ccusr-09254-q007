"""冻结签署会话：发起、签署、撤回、替换代表、发布的接口与服务层测试。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

DELEGATES = {
    "academic_affairs": "registrar-li",
    "college": "dean-wang",
    "audit": "auditor-zhao",
}


def _create_plan(client, plan=SHANGHAI_PLAN):
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text
    return plan["plan_version"]


def _seed_checkin(client, pv, eid="E-01", student="S1"):
    resp = client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                {
                    "event_id": eid,
                    "event_type": "checkin",
                    "student_id": student,
                    "payload": {
                        "activity_id": "A1",
                        "activity_type": "regular",
                        "check_in_at": "2024-03-15T08:00:00+08:00",
                        "check_out_at": "2024-03-15T10:00:00+08:00",
                    },
                }
            ]
        },
    )
    assert resp.status_code == 201, resp.text


def _seed_correction(client, pv, eid="E-09", seconds=3600):
    resp = client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                {
                    "event_id": eid,
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {"adjustment_seconds": seconds, "reason": "make-up"},
                }
            ]
        },
    )
    assert resp.status_code == 201, resp.text


def _initiate(client, pv, sid="SS-01", freeze_id="F-SIGNED", **overrides):
    body = {"freeze_id": freeze_id, "delegates": dict(DELEGATES), "created_by": "registrar-admin"}
    body.update(overrides)
    return client.post(f"/api/plans/{pv}/sign-sessions/{sid}", json=body)


def _sign(client, pv, sid, role, signer):
    return client.post(
        f"/api/plans/{pv}/sign-sessions/{sid}/signatures/{role}",
        json={"signer_id": signer},
    )


def _withdraw(client, pv, sid, role, signer):
    return client.post(
        f"/api/plans/{pv}/sign-sessions/{sid}/signatures/{role}/withdraw",
        json={"signer_id": signer},
    )


def _replace(client, pv, sid, role, new_delegate, reason="personnel change"):
    return client.post(
        f"/api/plans/{pv}/sign-sessions/{sid}/delegates/{role}",
        json={"delegate_id": new_delegate, "reason": reason, "actor_id": "hr-admin"},
    )


def _publish(client, pv, sid):
    return client.post(f"/api/plans/{pv}/sign-sessions/{sid}/publish")


def _sign_all(client, pv, sid, delegates=DELEGATES):
    for role, signer in delegates.items():
        resp = _sign(client, pv, sid, role, signer)
        assert resp.status_code == 201, resp.text


def test_full_lifecycle_reaches_quorum_and_publishes(client):
    pv = _create_plan(client)
    _seed_checkin(client, pv)

    initiated = _initiate(client, pv)
    assert initiated.status_code == 201, initiated.text
    view = initiated.json()
    assert view["status"] == "open"
    assert view["quorum"] == 3
    assert view["valid_votes"] == 0
    assert view["quorum_met"] is False
    assert view["delegates"] == DELEGATES
    assert view["event_cutoff_id"] == "E-01"
    assert view["snapshot"]["freeze_id"] == "F-SIGNED"
    assert view["snapshot"]["students"][0]["total_seconds"] == 7200

    _sign_all(client, pv, "SS-01")

    after = client.get(f"/api/plans/{pv}/sign-sessions/SS-01").json()
    assert after["valid_votes"] == 3
    assert after["quorum_met"] is True
    assert all(sig["valid"] for sig in after["signatures"])

    published = _publish(client, pv, "SS-01")
    assert published.status_code == 201, published.text
    freeze = published.json()
    assert freeze["freeze_id"] == "F-SIGNED"
    assert freeze["students"][0]["total_seconds"] == 7200

    # 发布后可通过既有冻结接口读取，内容与会话快照一致。
    stored = client.get(f"/api/plans/{pv}/freezes/F-SIGNED").json()
    assert stored == view["snapshot"]

    session = client.get(f"/api/plans/{pv}/sign-sessions/SS-01").json()
    assert session["status"] == "published"
    assert session["published_at"] is not None


def test_snapshot_fixed_at_initiation_and_signing_never_changes_content(client):
    pv = _create_plan(client)
    _seed_checkin(client, pv)
    fixed = _initiate(client, pv).json()["snapshot"]

    # 会话发起后到达的修正事件不影响已固定的快照内容。
    _seed_correction(client, pv)
    _sign_all(client, pv, "SS-01")
    _replace(client, pv, "SS-01", "college", "dean-sun", reason="conflict of interest")
    _sign(client, pv, "SS-01", "college", "dean-sun")

    view = client.get(f"/api/plans/{pv}/sign-sessions/SS-01").json()
    assert view["snapshot"] == fixed

    freeze = _publish(client, pv, "SS-01").json()
    assert freeze["students"][0]["total_seconds"] == 7200
    assert freeze["event_cutoff_id"] == "E-01"
    # 实时快照已含修正，但发布内容保持发起时的状态。
    live = client.get(f"/api/plans/{pv}/snapshot").json()
    assert live["students"][0]["total_seconds"] == 7200 + 3600


def test_reinitiate_same_session_is_idempotent(client):
    pv = _create_plan(client)
    _seed_checkin(client, pv)
    first = _initiate(client, pv).json()

    _seed_correction(client, pv)
    second = _initiate(client, pv, freeze_id="F-OTHER", quorum=1)
    assert second.status_code == 201
    assert second.json()["snapshot"] == first["snapshot"]
    assert second.json()["freeze_id"] == "F-SIGNED"
    assert second.json()["quorum"] == 3


def test_withdraw_before_publish_blocks_quorum_and_resign_restores(client):
    pv = _create_plan(client)
    _seed_checkin(client, pv)
    _initiate(client, pv)
    _sign_all(client, pv, "SS-01")

    withdrawn = _withdraw(client, pv, "SS-01", "audit", DELEGATES["audit"])
    assert withdrawn.status_code == 200, withdrawn.text
    view = withdrawn.json()
    assert view["valid_votes"] == 2
    assert view["quorum_met"] is False
    audit_sig = [s for s in view["signatures"] if s["role"] == "audit"][0]
    assert audit_sig["withdrawn_at"] is not None
    assert audit_sig["valid"] is False

    blocked = _publish(client, pv, "SS-01")
    assert blocked.status_code == 409
    assert "quorum" in blocked.json()["detail"]

    # 撤回后重新签署，签名重新生效。
    resigned = _sign(client, pv, "SS-01", "audit", DELEGATES["audit"])
    assert resigned.status_code == 201
    assert resigned.json()["quorum_met"] is True
    assert _publish(client, pv, "SS-01").status_code == 201


def test_withdraw_after_publish_rejected(client):
    pv = _create_plan(client)
    _seed_checkin(client, pv)
    _initiate(client, pv)
    _sign_all(client, pv, "SS-01")
    assert _publish(client, pv, "SS-01").status_code == 201

    resp = _withdraw(client, pv, "SS-01", "audit", DELEGATES["audit"])
    assert resp.status_code == 409
    # 发布后签署、替换代表同样被拒绝。
    assert _sign(client, pv, "SS-01", "audit", DELEGATES["audit"]).status_code == 409
    assert _replace(client, pv, "SS-01", "audit", "auditor-qian").status_code == 409
    # 重复发布幂等，返回同一冻结。
    assert _publish(client, pv, "SS-01").json()["freeze_id"] == "F-SIGNED"


def test_withdraw_unknown_signature_returns_404(client):
    pv = _create_plan(client)
    _initiate(client, pv)
    resp = _withdraw(client, pv, "SS-01", "audit", DELEGATES["audit"])
    assert resp.status_code == 404


def test_replace_delegate_invalidates_old_vote_but_keeps_content(client):
    pv = _create_plan(client)
    _seed_checkin(client, pv)
    fixed = _initiate(client, pv).json()["snapshot"]
    _sign_all(client, pv, "SS-01")

    replaced = _replace(client, pv, "SS-01", "college", "dean-sun", reason="利益冲突回避")
    assert replaced.status_code == 200, replaced.text
    view = replaced.json()
    # 原代表的签名仍在，但不再计入有效票。
    assert view["valid_votes"] == 2
    assert view["quorum_met"] is False
    old_sig = [s for s in view["signatures"] if s["signer_id"] == "dean-wang"][0]
    assert old_sig["valid"] is False
    assert view["delegates"]["college"] == "dean-sun"
    assert view["delegate_history"][0]["previous_delegate"] == "dean-wang"
    assert view["delegate_history"][0]["reason"] == "利益冲突回避"
    assert view["snapshot"] == fixed

    assert _publish(client, pv, "SS-01").status_code == 409

    # 新代表签署后达到法定人数，内容依旧不变。
    assert _sign(client, pv, "SS-01", "college", "dean-sun").status_code == 201
    freeze = _publish(client, pv, "SS-01").json()
    assert freeze == fixed


def test_delegate_replacement_changes_signing_permission(client):
    pv = _create_plan(client)
    _initiate(client, pv)
    _replace(client, pv, "SS-01", "audit", "auditor-qian")

    # 旧代表失去签署权限，新代表获得权限。
    assert _sign(client, pv, "SS-01", "audit", DELEGATES["audit"]).status_code == 403
    assert _sign(client, pv, "SS-01", "audit", "auditor-qian").status_code == 201


def test_conflict_of_interest_rejected_at_initiation_and_replacement(client):
    pv = _create_plan(client)
    bad = dict(DELEGATES)
    bad["audit"] = bad["college"]
    resp = _initiate(client, pv, delegates=bad)
    assert resp.status_code == 400
    assert "conflict of interest" in resp.json()["detail"]

    _initiate(client, pv)
    conflict = _replace(client, pv, "SS-01", "audit", DELEGATES["college"])
    assert conflict.status_code == 409


def test_initiate_validation_errors(client):
    pv = _create_plan(client)
    # 缺少必需角色。
    resp = _initiate(client, pv, delegates={"college": "dean-wang"})
    assert resp.status_code == 400
    # 法定人数越界。
    assert _initiate(client, pv, quorum=0).status_code == 422
    assert _initiate(client, pv, quorum=4).status_code == 400
    # ttl 与绝对截止时间互斥。
    assert _initiate(
        client, pv, ttl_seconds=60, expires_at="2030-01-01T00:00:00Z"
    ).status_code == 400
    # 未知方案。
    assert _initiate(client, "P-MISSING").status_code == 404


def test_non_delegate_cannot_sign_and_unknown_role_404(client):
    pv = _create_plan(client)
    _initiate(client, pv)
    assert _sign(client, pv, "SS-01", "audit", "intruder").status_code == 403
    assert _sign(client, pv, "SS-01", "board", "registrar-li").status_code == 404
    assert _replace(client, pv, "SS-01", "board", "x").status_code == 404
    assert _sign(client, "P-MISSING", "SS-01", "audit", "x").status_code == 404
    assert _sign(client, pv, "SS-MISSING", "audit", "x").status_code == 404


def test_duplicate_signature_counted_once(client):
    pv = _create_plan(client)
    _initiate(client, pv)
    first = _sign(client, pv, "SS-01", "audit", DELEGATES["audit"])
    second = _sign(client, pv, "SS-01", "audit", DELEGATES["audit"])
    assert first.status_code == 201
    assert second.status_code == 201

    view = client.get(f"/api/plans/{pv}/sign-sessions/SS-01").json()
    audit_sigs = [s for s in view["signatures"] if s["role"] == "audit"]
    assert len(audit_sigs) == 1
    assert view["valid_votes"] == 1


def test_custom_quorum_publishes_with_partial_roles(client):
    pv = _create_plan(client)
    _initiate(client, pv, quorum=2)
    _sign(client, pv, "SS-01", "academic_affairs", DELEGATES["academic_affairs"])
    assert _publish(client, pv, "SS-01").status_code == 409
    _sign(client, pv, "SS-01", "audit", DELEGATES["audit"])
    assert _publish(client, pv, "SS-01").status_code == 201


def test_expired_session_rejects_all_changes(client):
    pv = _create_plan(client)
    _seed_checkin(client, pv)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    resp = _initiate(client, pv, expires_at=past)
    assert resp.status_code == 201, resp.text

    view = client.get(f"/api/plans/{pv}/sign-sessions/SS-01").json()
    assert view["status"] == "expired"

    assert _sign(client, pv, "SS-01", "audit", DELEGATES["audit"]).status_code == 409
    assert _withdraw(client, pv, "SS-01", "audit", DELEGATES["audit"]).status_code == 409
    assert _replace(client, pv, "SS-01", "audit", "auditor-qian").status_code == 409
    assert _publish(client, pv, "SS-01").status_code == 409


def test_session_expires_between_initiation_and_signing(client):
    """服务层注入时间：会话在签署前过期。"""
    from app import services

    pv = _create_plan(client)
    start = datetime(2024, 9, 1, 8, 0, tzinfo=timezone.utc)
    later = start + timedelta(hours=2)

    db = TestSessionLocal()
    try:
        view, created = services.initiate_sign_session(
            db,
            plan_version=pv,
            session_id="SS-TTL",
            freeze_id="F-TTL",
            delegates=dict(DELEGATES),
            ttl_seconds=3600,
            now=start,
        )
        assert created is True
        assert view["status"] == "open"
        assert view["expires_at"] == "2024-09-01T09:00:00Z"

        with pytest.raises(services.SignSessionClosedError):
            services.sign_session(
                db, pv, "SS-TTL", "audit", DELEGATES["audit"], now=later
            )
        with pytest.raises(services.SignSessionClosedError):
            services.publish_sign_session(db, pv, "SS-TTL", now=later)

        expired = services.get_sign_session_view(db, pv, "SS-TTL", now=later)
        assert expired["status"] == "expired"
    finally:
        db.close()


def test_concurrent_signing_distinct_roles_all_counted(client):
    pv = _create_plan(client)
    _seed_checkin(client, pv)
    _initiate(client, pv)

    from app import services

    errors: list[Exception] = []

    def _worker(role: str, signer: str):
        session = TestSessionLocal()
        try:
            services.sign_session(session, pv, "SS-01", role, signer)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [
        threading.Thread(target=_worker, args=(role, signer))
        for role, signer in DELEGATES.items()
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    view = client.get(f"/api/plans/{pv}/sign-sessions/SS-01").json()
    assert view["valid_votes"] == 3
    assert len(view["signatures"]) == 3


def test_concurrent_duplicate_signing_keeps_single_row(client):
    pv = _create_plan(client)
    _initiate(client, pv)

    from app import services

    errors: list[Exception] = []

    def _worker():
        session = TestSessionLocal()
        try:
            services.sign_session(
                session, pv, "SS-01", "audit", DELEGATES["audit"]
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    view = client.get(f"/api/plans/{pv}/sign-sessions/SS-01").json()
    assert len(view["signatures"]) == 1
    assert view["valid_votes"] == 1


def test_concurrent_publish_creates_single_freeze(client):
    pv = _create_plan(client)
    _seed_checkin(client, pv)
    _initiate(client, pv)
    _sign_all(client, pv, "SS-01")

    from app import services

    results: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _worker():
        session = TestSessionLocal()
        try:
            snap = services.publish_sign_session(session, pv, "SS-01")
            with lock:
                results.append(snap.freeze_id)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert results == ["F-SIGNED"] * 4
    stored = client.get(f"/api/plans/{pv}/freezes/F-SIGNED").json()
    assert stored["students"][0]["total_seconds"] == 7200
    session = client.get(f"/api/plans/{pv}/sign-sessions/SS-01").json()
    assert session["status"] == "published"


def test_concurrent_initiate_only_one_session_created(client):
    pv = _create_plan(client)
    _seed_checkin(client, pv)

    from app import services

    created_flags: list[bool] = []
    lock = threading.Lock()

    def _worker():
        session = TestSessionLocal()
        try:
            _, created = services.initiate_sign_session(
                session,
                plan_version=pv,
                session_id="SS-RACE",
                freeze_id="F-RACE",
                delegates=dict(DELEGATES),
            )
            with lock:
                created_flags.append(created)
        finally:
            session.close()

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for c in created_flags if c) == 1
    assert sum(1 for c in created_flags if not c) == 3
    view = client.get(f"/api/plans/{pv}/sign-sessions/SS-RACE").json()
    assert view["freeze_id"] == "F-RACE"
