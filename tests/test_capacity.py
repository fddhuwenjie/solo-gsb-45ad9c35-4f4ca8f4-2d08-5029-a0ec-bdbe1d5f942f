"""孔位容量上限的端到端测试。

- 既有/新建孔位默认不限容；
- 通过 PUT /wells/{well}/capacity 设置或取消（null），低于当前体积返回 409；
- 补液与批次移液每一步按当时目标体积校验，批次任一步超限整批回滚；
- 撤销前核对快照恢复体积对当前上限；
- 容量变更与补液/批次/撤销经 BEGIN IMMEDIATE 串行。
"""

import sqlite3
import threading
from decimal import Decimal

from app import ledger


def register(client, well, sample, volume):
    resp = client.post("/wells", json={"well": well, "sample": sample, "volume": volume})
    assert resp.status_code == 201, resp.text
    return resp.json()


def set_capacity(client, well, capacity):
    return client.put(f"/wells/{well}/capacity", json={"capacity": capacity})


def amounts(body):
    return {c["sample"]: Decimal(c["amount"]) for c in body["composition"]}


def batch(client, request_key, operations):
    return client.post(
        "/batches", json={"request_key": request_key, "operations": operations}
    )


# --------------------------------------------------------- 默认不限与设置/取消


def test_wells_default_to_unlimited_and_expose_capacity_null(client):
    body = register(client, "A1", "S1", "100")
    assert body["capacity"] is None


def test_set_capacity_returns_state_and_get_reflects_it(client):
    register(client, "A1", "S1", "100")
    resp = set_capacity(client, "A1", "120.5")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["volume"] == "100"
    assert body["capacity"] == "120.5"
    assert client.get("/wells/A1").json()["capacity"] == "120.5"

    # 恰好等于上限合法
    assert set_capacity(client, "A1", "100").status_code == 200


def test_cancel_capacity_with_null_restores_unlimited(client):
    register(client, "A1", "S1", "100")
    assert set_capacity(client, "A1", "100").status_code == 200
    resp = set_capacity(client, "A1", None)
    assert resp.status_code == 200
    assert resp.json()["capacity"] is None
    # 取消后补液不再受限
    body = register(client, "A1", "S1", "500")
    assert body["volume"] == "600"


def test_capacity_must_be_positive(client):
    register(client, "A1", "S1", "100")
    assert set_capacity(client, "A1", "0").status_code == 422
    assert set_capacity(client, "A1", "-1").status_code == 422


def test_capacity_on_unknown_well_is_404(client):
    resp = set_capacity(client, "Z9", "10")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "孔位 Z9 尚未登记"


def test_capacity_below_current_volume_is_409_and_unchanged(client):
    register(client, "A1", "S1", "100")
    resp = set_capacity(client, "A1", "99.999999999")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["well"] == "A1"
    assert detail["current"] == "100"
    assert detail["capacity"] == "99.999999999"
    assert "容量未变更" in detail["message"]

    # 配置不变：仍是不限容，补液照常
    assert client.get("/wells/A1").json()["capacity"] is None
    assert register(client, "A1", "S1", "5")["volume"] == "105"


def test_capacity_accepts_decimal_string_not_float(client):
    register(client, "A1", "S1", "0.1")
    resp = set_capacity(client, "A1", "0.300000000")
    assert resp.status_code == 200
    register(client, "A1", "S1", "0.1")
    conflict = register_raw(client, "A1", "S1", "0.1")
    # 0.1+0.1+0.1 = 0.3 恰好；再补 0.1 才超限——这里直接验证 0.3 上限精确
    assert conflict.status_code == 201, conflict.text
    over = register_raw(client, "A1", "S1", "0.000000001")
    assert over.status_code == 409
    assert over.json()["detail"]["capacity"] == "0.3"


def register_raw(client, well, sample, volume):
    return client.post(
        "/wells", json={"well": well, "sample": sample, "volume": volume}
    )


# --------------------------------------------------------------- 补液超限


def test_top_up_over_capacity_is_409_and_state_unchanged(client):
    register(client, "A1", "S1", "100")
    set_capacity(client, "A1", "105")
    resp = register_raw(client, "A1", "S1", "6")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["well"] == "A1"
    assert detail["current"] == "100"
    assert detail["incoming"] == "6"
    assert detail["capacity"] == "105"

    # 液体、组成与容量均不变；恰好补到上限可以
    body = client.get("/wells/A1").json()
    assert body["volume"] == "100"
    assert body["capacity"] == "105"
    ok = register_raw(client, "A1", "S1", "5")
    assert ok.status_code == 201
    assert ok.json()["volume"] == "105"


def test_initial_registration_respects_capacity_only_when_already_set(client):
    """容量不能在登记前预设；登记时若孔位已有限制则按当时体积校验。"""
    register(client, "A1", "S1", "100")
    set_capacity(client, "A1", "100")
    assert register_raw(client, "A1", "S2", "0.000000001").status_code == 409


# ---------------------------------------------------------- 批次移液超限


def test_batch_destination_over_capacity_rolls_back_whole_batch(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    # B1 尚未存在：先由第一次注入创建，再为其设定 50 的上限
    resp = batch(
        client,
        "cap-seed",
        [{"source": "A1", "destination": "B1", "volume": "40"}],
    )
    assert resp.status_code == 201, resp.text
    set_capacity(client, "B1", "50")

    resp = batch(
        client,
        "cap-fail",
        [
            {"source": "A2", "destination": "B1", "volume": "5"},   # 40+5=45，OK
            {"source": "A2", "destination": "B1", "volume": "10"},  # 45+10=55，超限
        ],
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["well"] == "B1"
    assert detail["current"] == "45"
    assert detail["incoming"] == "10"
    assert detail["capacity"] == "50"
    assert "超过容量上限" in detail["message"]

    # 整批回滚：第一步也撤销，B1 仍是 40；A2 未被取走
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert client.get("/wells/A2").json()["volume"] == "100"

    failed = client.get(f"/batches/{detail['batch_id']}").json()
    assert failed["status"] == "failed"
    assert failed["operations"] == []

    # 失败批次消耗 request_key：重放返回该失败记录
    replay = batch(
        client,
        "cap-fail",
        [
            {"source": "A2", "destination": "B1", "volume": "5"},
            {"source": "A2", "destination": "B1", "volume": "10"},
        ],
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "failed"
    assert replay.json()["replayed"] is True


def test_batch_may_move_out_before_in_under_capacity(client):
    """同一批次允许先移出腾出空间再移入：每一步按当时目标体积判定。"""
    register(client, "A1", "S1", "100")
    # B1 初始 50，上限 50
    resp = batch(
        client,
        "b1-seed",
        [{"source": "A1", "destination": "B1", "volume": "50"}],
    )
    assert resp.status_code == 201
    set_capacity(client, "B1", "50")

    resp = batch(
        client,
        "out-then-in",
        [
            {"source": "B1", "destination": "C1", "volume": "20"},  # B1 50 -> 30
            {"source": "A1", "destination": "B1", "volume": "20"},  # B1 30 -> 50
        ],
    )
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/B1").json()["volume"] == "50"
    assert client.get("/wells/C1").json()["volume"] == "20"

    # 反向顺序（先移入）则第一步即超限并整批回滚
    resp = batch(
        client,
        "in-then-out",
        [
            {"source": "A1", "destination": "B1", "volume": "1"},   # 50+1 超限
            {"source": "B1", "destination": "C1", "volume": "20"},
        ],
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["well"] == "B1"
    assert detail["current"] == "50"
    assert detail["incoming"] == "1"
    assert client.get("/wells/B1").json()["volume"] == "50"
    assert client.get("/wells/C1").json()["volume"] == "20"  # 未受失败批次影响


def test_batch_failure_midway_restores_earlier_steps(client):
    """第 N 步超限：前 N-1 步的移动同样随事务回滚。"""
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "100")
    resp = batch(
        client,
        "seed2",
        [{"source": "A1", "destination": "B1", "volume": "40"}],
    )
    assert resp.status_code == 201
    set_capacity(client, "B1", "45")

    resp = batch(
        client,
        "mid-fail",
        [
            {"source": "A2", "destination": "B1", "volume": "5"},   # OK 45
            {"source": "A2", "destination": "D1", "volume": "30"},  # D1 30
            {"source": "A2", "destination": "B1", "volume": "1"},   # 45+1 超限
        ],
    )
    assert resp.status_code == 409
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert client.get("/wells/A2").json()["volume"] == "100"
    assert client.get("/wells/D1").status_code == 404  # 中途新建孔随回滚消失


def test_batch_exactly_at_capacity_is_allowed(client):
    register(client, "A1", "S1", "100")
    set_capacity(client, "B1", "40")
    resp = batch(
        client,
        "exact",
        [{"source": "A1", "destination": "B1", "volume": "40"}],
    )
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/B1").json()["volume"] == "40"


# ------------------------------------------------- 旧库迁移：既有孔位不限容


def test_legacy_database_migrates_with_unlimited_capacity(tmp_path, monkeypatch):
    """上线前的 wells 表（无 capacity 列）启动时自动加列，既有孔位仍不限容。"""
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("CREATE TABLE wells (well TEXT PRIMARY KEY, volume TEXT NOT NULL DEFAULT '0')")
        conn.execute("INSERT INTO wells (well, volume) VALUES ('A1', '100')")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("LINEAGE_DB", str(db_file))
    from app import db as db_module

    db_module.init_db()  # 幂等迁移
    conn = db_module.connect()
    try:
        row = conn.execute("SELECT volume, capacity FROM wells WHERE well = 'A1'").fetchone()
        assert row["volume"] == "100"
        assert row["capacity"] is None  # 既有数据默认不限
    finally:
        conn.close()

    # 迁移后容量端点与补液均可用，且仍不限容
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as legacy_client:
        body = legacy_client.get("/wells/A1").json()
        assert body["volume"] == "100"
        assert body["capacity"] is None
        resp = legacy_client.post(
            "/wells", json={"well": "A1", "sample": "S1", "volume": "999999"}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["volume"] == "1000099"


# ------------------------------------------------------------- 撤销与容量


def reverse(client, batch_id, request_key):
    return client.post(
        f"/batches/{batch_id}/reversals", json={"request_key": request_key}
    )


def test_reversal_rejected_when_snapshot_volume_exceeds_current_capacity(client):
    register(client, "A1", "S1", "100")
    resp = batch(
        client,
        "revcap-1",
        [{"source": "A1", "destination": "B1", "volume": "40"}],
    )
    bid = resp.json()["batch_id"]
    assert client.get("/wells/A1").json()["volume"] == "60"

    # 把 A1 上限降到 80：撤销会恢复到 100 -> 超限，拒绝整次撤销
    assert set_capacity(client, "A1", "80").status_code == 200
    resp = reverse(client, bid, "rev-revcap")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "capacity_exceeded"
    assert detail["over_capacity_wells"] == [
        {"well": "A1", "restored_volume": "100", "capacity": "80"}
    ]

    # 液体与审计记录都不变
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"
    assert client.get(f"/batches/{bid}").json()["reversed"] is False
    import os

    conn = sqlite3.connect(os.environ["LINEAGE_DB"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM reversals").fetchone()[0] == 0
    finally:
        conn.close()

    # 冲突不消耗撤销 request_key：放开容量后同键重试成功
    set_capacity(client, "A1", None)
    ok = reverse(client, bid, "rev-revcap")
    assert ok.status_code == 201, ok.text
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


def test_tightening_capacity_then_relaxing_allows_reversal(client):
    register(client, "A1", "S1", "100")
    bid = batch(
        client,
        "revcap-2",
        [{"source": "A1", "destination": "B1", "volume": "40"}],
    ).json()["batch_id"]
    # A1 当前 60：可把上限设为 60，但此时撤销恢复 100 会被拒
    set_capacity(client, "A1", "60")
    assert reverse(client, bid, "rev-tight").status_code == 409
    # 放宽到 100 后恢复恰好等于上限，允许
    set_capacity(client, "A1", "100")
    assert reverse(client, bid, "rev-tight").status_code == 201
    assert client.get("/wells/A1").json()["capacity"] == "100"


def test_capacity_change_does_not_block_reversal_and_is_preserved(client):
    """容量变更不记入孔位事件流：不构成撤销阻塞，且撤销后配置保留。"""
    register(client, "A1", "S1", "100")
    bid = batch(
        client,
        "revcap-3",
        [{"source": "A1", "destination": "B1", "volume": "40"}],
    ).json()["batch_id"]
    set_capacity(client, "A1", "200")
    set_capacity(client, "B1", "200")
    assert reverse(client, bid, "rev-cap-preserved").status_code == 201
    assert client.get("/wells/A1").json()["capacity"] == "200"
    # B1 随撤销被整体删除（快照中不存在），容量配置随之删除
    assert client.get("/wells/B1").status_code == 404


# ------------------------------------------------- 并发：容量变更串行于其他写


class _Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait_inside(self, timeout=10.0):
        assert self.entered.wait(timeout), "工作线程未在超时内进入门闩"


def _run_in_thread(target):
    result = {}

    def runner():
        try:
            result["value"] = target()
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    return thread, result


def _join(thread, result, timeout=30.0):
    thread.join(timeout)
    assert not thread.is_alive(), "工作线程超时未结束（可能死锁）"
    if "error" in result:
        raise result["error"]
    return result["value"]


def test_capacity_set_first_blocks_waiting_top_up(client, monkeypatch):
    """先提交的容量收紧：在锁上等待的补液按新上限校验并被拒。"""
    register(client, "A1", "S1", "100")
    gate = _Gate()
    original = ledger.set_capacity

    def gated_set_capacity(conn, well, capacity):
        gate.entered.set()
        assert gate.release.wait(10.0), "容量线程未被释放"
        return original(conn, well, capacity)

    monkeypatch.setattr(ledger, "set_capacity", gated_set_capacity)

    cap_thread, cap_result = _run_in_thread(lambda: set_capacity(client, "A1", "100"))
    gate.wait_inside()  # 容量变更已持写锁

    topup_thread, topup_result = _run_in_thread(
        lambda: register_raw(client, "A1", "S1", "1")
    )
    threading.Event().wait(0.3)
    assert topup_thread.is_alive(), "补液应在容量变更的写锁上排队"

    gate.release.set()
    cap_body = _join(cap_thread, cap_result)
    topup_resp = _join(topup_thread, topup_result)

    assert cap_body.json()["capacity"] == "100"
    assert topup_resp.status_code == 409, topup_resp.text
    detail = topup_resp.json()["detail"]
    assert detail["current"] == "100"
    assert detail["incoming"] == "1"
    assert detail["capacity"] == "100"
    assert client.get("/wells/A1").json()["volume"] == "100"


def test_top_up_first_then_capacity_tightening_rejected(client, monkeypatch):
    """先提交的补液决定状态：随后收紧到低于新体积的容量返回 409，配置不变。"""
    register(client, "A1", "S1", "100")
    gate = _Gate()
    original = ledger.register_initial_sample

    def gated_register(conn, well, sample, volume):
        gate.entered.set()
        assert gate.release.wait(10.0), "补液线程未被释放"
        return original(conn, well, sample, volume)

    monkeypatch.setattr(ledger, "register_initial_sample", gated_register)

    topup_thread, topup_result = _run_in_thread(
        lambda: register_raw(client, "A1", "S1", "5")
    )
    gate.wait_inside()  # 补液已持写锁

    cap_thread, cap_result = _run_in_thread(lambda: set_capacity(client, "A1", "102"))
    threading.Event().wait(0.3)
    assert cap_thread.is_alive(), "容量变更应在补液的写锁上排队"

    gate.release.set()
    topup_body = _join(topup_thread, topup_result)
    cap_resp = _join(cap_thread, cap_result)

    assert topup_body.json()["volume"] == "105"
    assert cap_resp.status_code == 409, cap_resp.text
    detail = cap_resp.json()["detail"]
    assert detail["current"] == "105"
    assert detail["capacity"] == "102"
    assert client.get("/wells/A1").json()["capacity"] is None


def test_capacity_change_serializes_with_batch(client, monkeypatch):
    """批次持锁期间容量收紧排队等待；批次按其开始时的不限容配置成功提交。"""
    register(client, "A1", "S1", "100")
    gate = _Gate()
    original = ledger.apply_transfer

    def gated_transfer(conn, source, destination, volume):
        gate.entered.set()
        assert gate.release.wait(10.0), "批次线程未被释放"
        return original(conn, source, destination, volume)

    monkeypatch.setattr(ledger, "apply_transfer", gated_transfer)

    batch_thread, batch_result = _run_in_thread(
        lambda: batch(
            client,
            "serial-batch",
            [{"source": "A1", "destination": "B1", "volume": "60"}],
        )
    )
    gate.wait_inside()

    cap_thread, cap_result = _run_in_thread(lambda: set_capacity(client, "B1", "10"))
    threading.Event().wait(0.3)
    assert cap_thread.is_alive()

    gate.release.set()
    batch_resp = _join(batch_thread, batch_result)
    cap_resp = _join(cap_thread, cap_result)

    # 批次先提交：B1=60；容量想设 10 低于 60 -> 409，容量保持不限
    assert batch_resp.status_code == 201, batch_resp.text
    assert cap_resp.status_code == 409
    assert client.get("/wells/B1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["capacity"] is None
