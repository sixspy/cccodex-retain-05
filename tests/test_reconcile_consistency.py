"""对账一致性与写锁测试：

- 读页冻结快照：对账卡片 / 执行列表 / 分区详情同一读点，计数口径唯一
- 报告数字与分区行数对齐，混数即拦（事务回滚，不落脏报告）
- 两名值班并发写：应用级互斥锁快速失败 409，不产生双写
"""
import threading

import pytest

from app.db import WriteLockedError, write_transaction
from app.services.reconcile import CountDriftError, count_partition_states, run_reconcile


def _scan_and_execute_one(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    pending = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    assert pending
    jid = pending[0]["id"]
    authed.post(f"/api/judgments/{jid}/execute")
    return jid


def test_snapshot_freezes_single_read_point_for_three_blocks(authed):
    _scan_and_execute_one(authed)
    r = authed.get("/api/reconcile/snapshot")
    assert r.status_code == 200
    snap = r.json()["snapshot"]

    # 三块同动：一个接口同时给出卡片计数、执行列表、分区详情
    assert "snapshot_at" in snap
    assert snap["counts"]["partition_total"] == len(snap["partitions"])
    assert len(snap["execute_runs"]) >= 1
    assert snap["count_aligned"] is True
    assert "同一读事务" in snap["count_basis"]

    # 卡片四态合计 == 分区详情行数，口径不拧巴
    c = snap["counts"]
    assert (
        c["online"] + c["archived"] + c["destroyed"] + c["reconciling"]
        == c["partition_total"]
    )
    state_tally = {}
    for p in snap["partitions"]:
        state_tally[p["storage_state"]] = state_tally.get(p["storage_state"], 0) + 1
    assert c["online"] == state_tally.get("在线", 0)
    assert c["archived"] == state_tally.get("已归档", 0)
    assert c["destroyed"] == state_tally.get("已销毁", 0)
    assert c["reconciling"] == state_tally.get("裁撤中", 0)


def test_report_counts_align_with_partition_rows(authed):
    _scan_and_execute_one(authed)
    r = authed.post("/api/reconcile-reports", json={"as_of_date": "2026-11-16"})
    assert r.status_code == 200
    report = r.json()["report"]

    parts = authed.get("/api/partitions").json()["items"]
    tally = {}
    for p in parts:
        tally[p["storage_state"]] = tally.get(p["storage_state"], 0) + 1

    assert report["online_count"] == tally.get("在线", 0)
    assert report["archived_count"] == tally.get("已归档", 0)
    assert report["destroyed_count"] == tally.get("已销毁", 0)
    assert report["reconciling_count"] == tally.get("裁撤中", 0)
    assert report["partition_total"] == len(parts)
    # 四态之和必须等于报告里的分区合计
    assert (
        report["online_count"]
        + report["archived_count"]
        + report["destroyed_count"]
        + report["reconciling_count"]
        == report["partition_total"]
    )
    assert "裁撤中" in report["summary"]
    assert report["snapshot_at"]

    # 库里读回的报告行与接口返回一致（落库可验）
    rows = authed.get("/api/reconcile-reports").json()["items"]
    assert rows[0]["id"] == report["id"]
    assert rows[0]["partition_total"] == report["partition_total"]


def test_write_lock_blocks_second_writer_with_409(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    pending = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    jid = pending[0]["id"]

    holder = threading.Event()
    release = threading.Event()

    def hold_lock():
        with write_transaction() as conn:
            holder.set()
            release.wait(timeout=5)
            # 持锁期间顺手做一笔真实落笔，释放后随上下文一起提交
            conn.execute(
                "UPDATE scan_jobs SET message = message WHERE id = "
                "(SELECT MAX(id) FROM scan_jobs)"
            )

    t = threading.Thread(target=hold_lock)
    t.start()
    assert holder.wait(timeout=5)
    try:
        # 第二名值班此时点裁撤：应用锁非阻塞，立刻 409
        r = authed.post(f"/api/judgments/{jid}/execute")
        assert r.status_code == 409
        assert "写操作进行中" in r.json()["detail"]

        # 点对账落库同样被拦
        r2 = authed.post("/api/reconcile-reports", json={"as_of_date": "2026-11-16"})
        assert r2.status_code == 409
        assert "写操作进行中" in r2.json()["detail"]

        # 被拦期间没有产生任何对账报告（无双写）
        assert authed.get("/api/reconcile-reports").json()["items"] == []
    finally:
        release.set()
        t.join()


def test_concurrent_reconcile_writes_only_one_report(authed):
    # 确定性并发：线程 A 持写锁先落一份报告（未释放），
    # 线程 B 走真实 HTTP 抢锁必须 409；锁释放后再落一份，库里恰好两份且不重不漏。
    entered = threading.Event()
    release = threading.Event()
    thread_error = []

    def writer_a():
        try:
            with write_transaction() as conn:
                run_reconcile(conn, "2026-11-16")
                entered.set()
                release.wait(timeout=5)
        except Exception as exc:  # noqa: BLE001
            thread_error.append(exc)

    t = threading.Thread(target=writer_a)
    t.start()
    assert entered.wait(timeout=5), "持锁线程未能进入写事务"
    try:
        r = authed.post(
            "/api/reconcile-reports", json={"as_of_date": "2026-11-16"}
        )
        assert r.status_code == 409
        # 持锁未提交时，库里还看不到那份报告，更不会出现 B 的双写
        assert authed.get("/api/reconcile-reports").json()["items"] == []
    finally:
        release.set()
        t.join()

    assert not thread_error
    # A 提交后可见且仅一份
    items = authed.get("/api/reconcile-reports").json()["items"]
    assert len(items) == 1

    # 锁释放后 B 重试成功——是「快速失败后人工重试」，不是排队双写
    again = authed.post(
        "/api/reconcile-reports", json={"as_of_date": "2026-11-16"}
    )
    assert again.status_code == 200
    assert len(authed.get("/api/reconcile-reports").json()["items"]) == 2


def test_count_drift_is_blocked_and_rolled_back(authed, monkeypatch):
    # 直接在服务层注入「第二个读点数变了」的漂移：模拟落笔瞬间有人裁撤，
    # 复核口径对不上 -> CountDriftError，且报告不得落库。
    import app.services.reconcile as rc

    real = rc._build_metrics
    calls = {"n": 0}

    def drifting(conn):
        calls["n"] += 1
        m = real(conn)
        if calls["n"] == 2:  # _assert_aligned 里的复核读点
            m = dict(m)
            m["online"] = m["online"] + 999
        return m

    monkeypatch.setattr(rc, "_build_metrics", drifting)

    with pytest.raises(CountDriftError):
        with write_transaction() as conn:
            run_reconcile(conn, "2026-11-16")

    # 事务已回滚：库里查不到任何报告
    assert authed.get("/api/reconcile-reports").json()["items"] == []


def test_single_counting_function_is_the_only_basis(authed, monkeypatch):
    # 守卫第一道：存储态合计与分区总行数不一致时直接拦
    import app.services.reconcile as rc

    def fake_states(conn):
        states = count_partition_states(conn)
        states["在线"] += 1  # 制造与 COUNT(*) 对不上的混数
        return states

    monkeypatch.setattr(rc, "count_partition_states", fake_states)
    with pytest.raises(CountDriftError):
        with write_transaction() as conn:
            run_reconcile(conn, "2026-11-16")

    assert authed.get("/api/reconcile-reports").json()["items"] == []


def test_write_lock_helper_raises_when_contended():
    # 单元级锁语义：重入抢锁必抛 WriteLockedError
    with write_transaction():
        with pytest.raises(WriteLockedError):
            with write_transaction():
                pass
