"""无提单禁建门闸（提单案戊 · 第5号）。

口径（甲路径，全仓唯一写入口径）：提单册无匹配行时，过磅正式册零增并拒收；
无影子票、无晋升、无第三路。双会话同提单号抢建须占提单锁 bl:<提单号>，
后到只留争用痕。本文件先红后绿，未见红不得改业务码。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin", password: str = "admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _lane_id(c: TestClient, h: dict, lane_code: str) -> int:
    lanes = c.get("/api/lanes", headers=h).json()["items"]
    return next(x for x in lanes if x["lane_code"] == lane_code)["id"]


def _formal_count(bl_ref: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM weigh_tickets WHERE bl_ref=?", (bl_ref,)
        ).fetchone()["c"]


# 失败测甲：无提单仍能建正式票（期望修复后 4xx 拒收、正式册零增、偏航入审计）
def test_05a_戊_无提单建票拒收且正式册零增():
    c = TestClient(app)
    h = _auth(c)
    lane_id = _lane_id(c, h, "LN-N-01-IN")
    before = c.get("/api/dashboard", headers=h).json()["tickets"]

    res = c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": lane_id,
            "bl_ref": "BL-E55",
            "vehicle_plate": "测E·5501",
            "axle_weights": [{"axle": 1, "w": 9.0}, {"axle": 2, "w": 11.0}],
            "ticket_code": "PL-E55",
        },
    )
    assert res.status_code in (409, 422), res.text
    assert _formal_count("BL-E55") == 0
    after = c.get("/api/dashboard", headers=h).json()["tickets"]
    assert after == before

    # 门闸须在正式册写库层同源封死：绕过服务直插无提单正式票必被外键拒收
    with pytest.raises(sqlite3.IntegrityError):
        with connect() as raw:
            raw.execute(
                """INSERT INTO weigh_tickets(ticket_code,lane_id,bl_ref,vehicle_plate,
                   axle_weights,gross_t,generation,status,adjudicated_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "PL-E55-RAW", lane_id, "BL-E55", "测E·RAW", "[]", 20.0,
                    1, "已裁定", None, "2026-11-24T00:00:00",
                ),
            )
            raw.commit()
    assert _formal_count("BL-E55") == 0

    # 偏航须留痕：无提单禁建偏航入审计轨迹
    audit = c.get("/api/audit", headers=h).json()["items"]
    assert any(
        x["action"] == "无提单禁建偏航" and "BL-E55" in (x["target"] or "")
        for x in audit
    ), audit


# 失败测乙：无提单种子经母仓种子写路径打出建票假绿，对账仍累计该票毛重
# （期望修复后：种子/直写同口径拦截，五页同源皆无此票，事后补登记也不追溯累计）
def test_05b_戊_无单种子不入对账毛重与五页同源(tmp_path, monkeypatch):
    import db as dbmod

    no_bl_seed = [
        {
            "ticket_code": "PL-E55",
            "lane_code": "LN-N-01-IN",
            "bl_ref": "BL-E55",
            "vehicle_plate": "测E·种子55",
            "axle_weights": [{"axle": 1, "w": 8.0}, {"axle": 2, "w": 12.0}],
            "gross_t": 20.0,
            "status": "已裁定",
            "created_at": "2026-11-24T08:00:00",
        }
    ]

    def _fake_load(name: str):
        return no_bl_seed if name == "tickets.json" else []

    monkeypatch.setattr(dbmod, "_load_json", _fake_load)
    with connect() as conn:
        dbmod.seed_tickets(conn)
        conn.commit()

    c = TestClient(app)
    h = _auth(c)
    lane_id = _lane_id(c, h, "LN-N-01-IN")

    # 门闸契约：无提单种子不得在正式册留下任何票
    assert _formal_count("BL-E55") == 0

    # API 写口同口径拒收
    rejected = c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": lane_id,
            "bl_ref": "BL-E55",
            "vehicle_plate": "测E·5502",
            "axle_weights": [{"axle": 1, "w": 8.0}, {"axle": 2, "w": 12.0}],
            "ticket_code": "PL-E55-API",
        },
    )
    assert rejected.status_code in (409, 422)

    # 提单页空
    assert c.get("/api/bl/BL-E55", headers=h).status_code == 404
    # 对账页：全量清单无 BL-E55
    codes = [x["bl_code"] for x in c.get("/api/reconcile", headers=h).json()["items"]]
    assert "BL-E55" not in codes
    # 过磅页：正式票清单无 PL-E55
    ticket_codes = [t["ticket_code"] for t in c.get("/api/tickets", headers=h).json()["items"]]
    assert "PL-E55" not in ticket_codes
    # 车道页：车道近期票无 PL-E55
    detail = c.get(f"/api/lanes/{lane_id}", headers=h).json()
    assert all(t["ticket_code"] != "PL-E55" for t in detail["recent_tickets"])
    # 总览页票量仍为夹具基线 4，无单票不得计入在港票
    dash = c.get("/api/dashboard", headers=h).json()
    assert dash["tickets"] == 4

    # 事后补登记提单：孤儿票本就不存在，对账须零累计、不得追溯毛重
    reg = c.post(
        "/api/bl",
        headers=h,
        json={
            "bl_code": "BL-E55",
            "cargo_desc": "案戊补登记货",
            "declared_gross_t": 20.0,
            "vessel_voyage": "案戊轮/0555",
        },
    )
    assert reg.status_code == 200
    recon = c.get("/api/reconcile/BL-E55", headers=h).json()
    assert recon["ticket_sum_t"] == 0
    assert recon["tickets"] == []
    assert recon["reason"] == "无过磅票"


# 争用失败测：双会话同提单号抢建，提单锁 bl:<提单号> 串行、后到只留争用痕
def test_05c_戊_双会话同提单号抢建只留争用痕():
    c = TestClient(app)
    h = _auth(c)
    h2 = _auth(c, "writer", "writer123")
    barrier = threading.Barrier(2)
    results: list[tuple[str, int]] = []

    def worker(user: str, password: str, lane_code: str, ticket_code: str, bl_ref: str):
        cc = TestClient(app)
        hh = _auth(cc, user, password)
        lane_id = _lane_id(cc, hh, lane_code)
        barrier.wait()
        r = cc.post(
            "/api/tickets",
            headers=hh,
            json={
                "lane_id": lane_id,
                "bl_ref": bl_ref,
                "vehicle_plate": f"测E·{user}",
                "axle_weights": [{"axle": 1, "w": 7.0}, {"axle": 2, "w": 9.0}],
                "ticket_code": ticket_code,
            },
        )
        results.append((user, r.status_code))

    # 阶段一：无提单 BL-E55 双会话跨车道并发——提单锁串行，两者皆不得建正式票
    t1 = threading.Thread(
        target=worker, args=("admin", "admin123", "LN-N-01-IN", "PL-E55-A", "BL-E55")
    )
    t2 = threading.Thread(
        target=worker, args=("writer", "writer123", "LN-S-01-OUT", "PL-E55-B", "BL-E55")
    )
    t1.start(); t2.start(); t1.join(); t2.join()
    assert len(results) == 2
    assert all(code in (409, 422) for _, code in results)
    assert _formal_count("BL-E55") == 0
    # 无提单门闸留偏航痕（至少一个会话走进门闸）
    audit = c.get("/api/audit", headers=h).json()["items"]
    assert any(x["action"] == "无提单禁建偏航" for x in audit)

    # 阶段二：writer 仅预占提单锁（不碰任何车道锁），两会话跨车道并发——
    # 确定性复现提单锁争用：writer 重入建票成功，admin 后到 409 且争用痕落盘
    pre = c.post(
        "/api/locks/acquire", headers=h2, json={"lock_key": "bl:BL-2026-0101"}
    )
    assert pre.status_code == 200
    results.clear()
    t3 = threading.Thread(
        target=worker, args=("admin", "admin123", "LN-N-01-IN", "WT-RACE-A", "BL-2026-0101")
    )
    t4 = threading.Thread(
        target=worker, args=("writer", "writer123", "LN-S-01-OUT", "WT-RACE-B", "BL-2026-0101")
    )
    t3.start(); t4.start(); t3.join(); t4.join()
    assert dict(results).get("admin") == 409, results
    assert dict(results).get("writer") == 200, results
    with connect() as conn:
        won = conn.execute(
            "SELECT COUNT(*) c FROM weigh_tickets WHERE ticket_code IN ('WT-RACE-A','WT-RACE-B')"
        ).fetchone()["c"]
    assert won == 1
    assert any(
        x["lock_key"] == "bl:BL-2026-0101" and x["actor"] == "admin"
        for x in c.get("/api/contention", headers=h).json()["items"]
    )

    # 阶段三：无预占、跨车道自由并发——提单锁库级串行，同提单号仍只许一票落地
    results.clear()
    t5 = threading.Thread(
        target=worker, args=("admin", "admin123", "LN-N-01-IN", "WT-RACE-C", "BL-2026-0101")
    )
    t6 = threading.Thread(
        target=worker, args=("writer", "writer123", "LN-S-01-OUT", "WT-RACE-D", "BL-2026-0101")
    )
    t5.start(); t6.start(); t5.join(); t6.join()
    assert sorted(code for _, code in results) == [200, 409], results
    with connect() as conn:
        won2 = conn.execute(
            "SELECT COUNT(*) c FROM weigh_tickets WHERE ticket_code IN ('WT-RACE-C','WT-RACE-D')"
        ).fetchone()["c"]
    assert won2 == 1
    # 争用痕只记不写业务：该提单正式票为夹具一票 + 抢建赢者两票
    assert _formal_count("BL-2026-0101") == 3
