"""服务端业务模块。"""

from __future__ import annotations

import threading

from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

DELEGATES = {"registrar": "reg-1", "college": "col-1", "audit": "aud-1"}


def _create_plan(client, plan=SHANGHAI_PLAN):
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text


def _seed_frozen_plan(client):
    """创建方案、导入一条签到事件并冻结 F-01。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                {
                    "event_id": "E-01",
                    "event_type": "checkin",
                    "student_id": "S1",
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
    resp = client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    assert resp.status_code == 201, resp.text
    return pv


def _initiate(client, pv, session_id="SS-1", freeze_id="F-01", **overrides):
    body = {"initiator": "registrar-office", "delegates": dict(DELEGATES)}
    body.update(overrides)
    return client.post(
        f"/api/plans/{pv}/freezes/{freeze_id}/sign-sessions/{session_id}",
        json=body,
    )


def _sign(client, pv, role, signer, session_id="SS-1"):
    return client.post(
        f"/api/plans/{pv}/sign-sessions/{session_id}/sign",
        json={"role": role, "signer_id": signer},
    )


def _withdraw(client, pv, role, signer, session_id="SS-1"):
    return client.post(
        f"/api/plans/{pv}/sign-sessions/{session_id}/withdraw",
        json={"role": role, "signer_id": signer},
    )


def _replace(client, pv, role, delegate_id, conflict=False, session_id="SS-1"):
    return client.post(
        f"/api/plans/{pv}/sign-sessions/{session_id}/delegates/{role}",
        json={"delegate_id": delegate_id, "conflict_of_interest": conflict},
    )


def _publish(client, pv, session_id="SS-1"):
    return client.post(f"/api/plans/{pv}/sign-sessions/{session_id}/publish")


def _state(client, pv, session_id="SS-1"):
    resp = client.get(f"/api/plans/{pv}/sign-sessions/{session_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _sign_all(client, pv, session_id="SS-1", delegates=DELEGATES):
    for role, signer in delegates.items():
        resp = _sign(client, pv, role, signer, session_id)
        assert resp.status_code == 201, resp.text


def test_full_signing_flow_reaches_quorum_and_publishes(client):
    pv = _seed_frozen_plan(client)
    resp = _initiate(client, pv)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "open"
    assert body["expired"] is False
    assert body["quorum"] == 3
    assert body["valid_votes"] == 0
    assert len(body["content_hash"]) == 64
    assert {d["role"] for d in body["delegates"]} == set(DELEGATES)

    _sign_all(client, pv)
    state = _state(client, pv)
    assert state["valid_votes"] == 3
    assert all(s["counts_toward_quorum"] for s in state["signatures"])

    published = _publish(client, pv)
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["published_at"] is not None

    # 发布后冻结快照内容保持不变。
    freeze = client.get(f"/api/plans/{pv}/freezes/F-01").json()
    assert freeze["students"][0]["total_seconds"] == 7200


def test_initiate_requires_existing_plan_and_freeze(client):
    resp = _initiate(client, "P-MISSING")
    assert resp.status_code == 404
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    resp = _initiate(client, pv, freeze_id="F-MISSING")
    assert resp.status_code == 404


def test_initiate_validates_delegates_and_quorum(client):
    pv = _seed_frozen_plan(client)
    resp = _initiate(client, pv, delegates={"registrar": "reg-1"})
    assert resp.status_code == 422
    resp = _initiate(client, pv, quorum=4)
    assert resp.status_code == 422
    resp = _initiate(client, pv, quorum=0)
    assert resp.status_code == 422


def test_initiate_is_idempotent_and_keeps_first_config(client):
    pv = _seed_frozen_plan(client)
    first = _initiate(client, pv, quorum=2)
    assert first.status_code == 201
    second = _initiate(
        client, pv, quorum=3, delegates={"registrar": "x", "college": "y", "audit": "z"}
    )
    assert second.status_code == 201
    assert second.json()["quorum"] == 2
    assert {d["delegate_id"] for d in second.json()["delegates"]} == set(
        DELEGATES.values()
    )


def test_duplicate_signature_is_idempotent_and_counts_once(client):
    pv = _seed_frozen_plan(client)
    _initiate(client, pv)
    first = _sign(client, pv, "registrar", "reg-1")
    assert first.status_code == 201
    second = _sign(client, pv, "registrar", "reg-1")
    assert second.status_code == 201
    state = _state(client, pv)
    assert state["valid_votes"] == 1
    active = [s for s in state["signatures"] if s["status"] == "active"]
    assert len(active) == 1
    assert active[0]["signer_id"] == "reg-1"


def test_concurrent_signing_same_delegate_yields_single_vote(client):
    pv = _seed_frozen_plan(client)
    _initiate(client, pv)

    from app import services

    errors: list[Exception] = []
    lock = threading.Lock()

    def _sign_worker():
        session = TestSessionLocal()
        try:
            services.sign_session(
                session,
                plan_version=pv,
                session_id="SS-1",
                role="registrar",
                signer_id="reg-1",
            )
        except Exception as exc:  # pragma: no cover - 失败时记录
            with lock:
                errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_sign_worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    state = _state(client, pv)
    assert state["valid_votes"] == 1
    active = [s for s in state["signatures"] if s["status"] == "active"]
    assert len(active) == 1


def test_concurrent_signing_distinct_roles_all_count(client):
    pv = _seed_frozen_plan(client)
    _initiate(client, pv)

    from app import services

    errors: list[Exception] = []
    lock = threading.Lock()

    def _sign_worker(role, signer):
        session = TestSessionLocal()
        try:
            services.sign_session(
                session,
                plan_version=pv,
                session_id="SS-1",
                role=role,
                signer_id=signer,
            )
        except Exception as exc:  # pragma: no cover - 失败时记录
            with lock:
                errors.append(exc)
        finally:
            session.close()

    threads = [
        threading.Thread(target=_sign_worker, args=(role, signer))
        for role, signer in DELEGATES.items()
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    state = _state(client, pv)
    assert state["valid_votes"] == 3
    published = _publish(client, pv)
    assert published.status_code == 200


def test_non_delegate_cannot_sign_and_permission_moves_with_replacement(client):
    pv = _seed_frozen_plan(client)
    _initiate(client, pv)

    # 非代表签署被拒绝。
    resp = _sign(client, pv, "registrar", "intruder")
    assert resp.status_code == 403

    # 原代表签署有效，计一票。
    assert _sign(client, pv, "registrar", "reg-1").status_code == 201
    assert _state(client, pv)["valid_votes"] == 1

    # 替换代表后：旧代表失去权限，其签名被取代不再计票。
    resp = _replace(client, pv, "registrar", "reg-2")
    assert resp.status_code == 200
    state = resp.json()
    assert state["valid_votes"] == 0
    old = [s for s in state["signatures"] if s["signer_id"] == "reg-1"]
    assert old[0]["status"] == "superseded"
    assert old[0]["counts_toward_quorum"] is False

    assert _sign(client, pv, "registrar", "reg-1").status_code == 403
    assert _sign(client, pv, "registrar", "reg-2").status_code == 201
    assert _state(client, pv)["valid_votes"] == 1


def test_conflict_of_interest_invalidates_vote_without_changing_content(client):
    pv = _seed_frozen_plan(client)
    _initiate(client, pv)
    _sign_all(client, pv)
    before = _state(client, pv)
    assert before["valid_votes"] == 3
    content_hash = before["content_hash"]

    # 标记学院代表存在利益冲突：该票失效，但内容与代表身份不变。
    resp = _replace(client, pv, "college", "col-1", conflict=True)
    assert resp.status_code == 200
    state = resp.json()
    assert state["valid_votes"] == 2
    assert state["content_hash"] == content_hash
    college_sig = [
        s for s in state["signatures"] if s["role"] == "college"
    ][0]
    assert college_sig["status"] == "active"
    assert college_sig["counts_toward_quorum"] is False

    # 法定人数未满，发布被拒绝。
    assert _publish(client, pv).status_code == 409

    # 冻结快照内容不受影响。
    freeze = client.get(f"/api/plans/{pv}/freezes/F-01").json()
    assert freeze["students"][0]["total_seconds"] == 7200

    # 解除冲突标记后原签名恢复计票，可发布。
    resp = _replace(client, pv, "college", "col-1", conflict=False)
    assert resp.json()["valid_votes"] == 3
    assert _publish(client, pv).status_code == 200


def test_withdrawal_only_valid_before_publish(client):
    pv = _seed_frozen_plan(client)
    _initiate(client, pv)
    _sign_all(client, pv)

    # 发布前撤回：有效票减少。
    resp = _withdraw(client, pv, "audit", "aud-1")
    assert resp.status_code == 200
    assert resp.json()["valid_votes"] == 2
    assert _publish(client, pv).status_code == 409

    # 撤回后可重新签署。
    assert _sign(client, pv, "audit", "aud-1").status_code == 201
    assert _state(client, pv)["valid_votes"] == 3

    # 发布后撤回不再有效，签署与换人也被拒绝。
    assert _publish(client, pv).status_code == 200
    assert _withdraw(client, pv, "audit", "aud-1").status_code == 409
    assert _sign(client, pv, "audit", "aud-1").status_code == 409
    assert _replace(client, pv, "audit", "aud-2").status_code == 409
    state = _state(client, pv)
    assert state["valid_votes"] == 3
    assert state["status"] == "published"


def test_withdraw_requires_active_signature(client):
    pv = _seed_frozen_plan(client)
    _initiate(client, pv)
    assert _withdraw(client, pv, "audit", "aud-1").status_code == 404
    _sign(client, pv, "audit", "aud-1")
    assert _withdraw(client, pv, "audit", "aud-1").status_code == 200
    assert _withdraw(client, pv, "audit", "aud-1").status_code == 404


def test_publish_requires_quorum(client):
    pv = _seed_frozen_plan(client)
    _initiate(client, pv)
    _sign(client, pv, "registrar", "reg-1")
    _sign(client, pv, "college", "col-1")
    resp = _publish(client, pv)
    assert resp.status_code == 409
    assert "quorum" in resp.json()["detail"]
    _sign(client, pv, "audit", "aud-1")
    assert _publish(client, pv).status_code == 200


def test_custom_quorum_allows_earlier_publish(client):
    pv = _seed_frozen_plan(client)
    _initiate(client, pv, quorum=2)
    _sign(client, pv, "registrar", "reg-1")
    assert _publish(client, pv).status_code == 409
    _sign(client, pv, "college", "col-1")
    assert _publish(client, pv).status_code == 200


def test_publish_is_idempotent(client):
    pv = _seed_frozen_plan(client)
    _initiate(client, pv)
    _sign_all(client, pv)
    first = _publish(client, pv)
    assert first.status_code == 200
    second = _publish(client, pv)
    assert second.status_code == 200
    assert second.json()["published_at"] == first.json()["published_at"]


def test_concurrent_publish_transitions_once(client):
    pv = _seed_frozen_plan(client)
    _initiate(client, pv)
    _sign_all(client, pv)

    from app import services

    outcomes: list[dict] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _publish_worker():
        session = TestSessionLocal()
        try:
            state = services.publish_sign_session(
                session, plan_version=pv, session_id="SS-1"
            )
            with lock:
                outcomes.append(state)
        except Exception as exc:  # pragma: no cover - 失败时记录
            with lock:
                errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_publish_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(outcomes) == 4
    assert all(o["status"] == "published" for o in outcomes)
    assert len({o["published_at"] for o in outcomes}) == 1
    assert _state(client, pv)["status"] == "published"


def test_expired_session_rejects_all_mutations(client):
    pv = _seed_frozen_plan(client)
    resp = _initiate(client, pv, expires_at="2020-01-01T00:00:00Z")
    assert resp.status_code == 201
    state = _state(client, pv)
    assert state["expired"] is True
    assert state["status"] == "open"

    assert _sign(client, pv, "registrar", "reg-1").status_code == 409
    assert _withdraw(client, pv, "registrar", "reg-1").status_code == 409
    assert _replace(client, pv, "registrar", "reg-2").status_code == 409
    assert _publish(client, pv).status_code == 409


def test_expired_session_can_be_replaced_by_new_session(client):
    pv = _seed_frozen_plan(client)
    _initiate(client, pv, session_id="SS-OLD", expires_at="2020-01-01T00:00:00Z")
    _initiate(client, pv, session_id="SS-NEW")
    _sign_all(client, pv, session_id="SS-NEW")
    assert _publish(client, pv, session_id="SS-NEW").status_code == 200
    assert _state(client, pv, "SS-OLD")["status"] == "open"
