"""过磅票：轴重采集与裁定入闸。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit, write_tx
from locks import release, try_acquire
from models import TicketIn
from validators import (
    adjudicate_overload,
    validate_axle_count,
    validate_axle_weights,
    validate_gross_cap,
    validate_lane_open,
)

router = APIRouter(tags=["tickets"])


class _NoBlGate(Exception):
    """内部信号：触发无提单禁建门闸，业务事务回滚后另记偏航。"""


def _next_ticket_code(conn, lane_id: int) -> str:
    lane = conn.execute(
        "SELECT lane_code FROM weigh_lanes WHERE id=?", (lane_id,)
    ).fetchone()
    prefix = lane["lane_code"] if lane else f"LN-{lane_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM weigh_tickets WHERE lane_id=?", (lane_id,)
    ).fetchone()["c"]
    return f"WT-{prefix}-{n + 1:03d}"


@router.get("/api/tickets")
def list_tickets(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT t.*, l.lane_code, b.bridge_code FROM weigh_tickets t
               JOIN weigh_lanes l ON l.id=t.lane_id
               JOIN weigh_bridges b ON b.id=l.bridge_id"""
        if status:
            rows = conn.execute(
                q + " WHERE t.status=? ORDER BY t.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY t.id DESC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["axle_weights"] = json.loads(d["axle_weights"] or "[]")
            items.append(d)
        return {"items": items}


@router.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT t.*, l.lane_code, l.overload_threshold_t, b.name bridge_name
               FROM weigh_tickets t
               JOIN weigh_lanes l ON l.id=t.lane_id
               JOIN weigh_bridges b ON b.id=l.bridge_id
               WHERE t.id=?""",
            (ticket_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "过磅票不存在")
        data = dict(row)
        data["axle_weights"] = json.loads(data["axle_weights"] or "[]")
        holds = conn.execute(
            "SELECT * FROM overload_holds WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM gate_seals WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        releases = conn.execute(
            "SELECT * FROM gate_releases WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        return {
            "ticket": data,
            "overload_holds": [dict(h) for h in holds],
            "gate_seals": [dict(s) for s in seals],
            "gate_releases": [dict(r) for r in releases],
        }


@router.post("/api/tickets")
def create_ticket(body: TicketIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lane_lock = f"lane:{body.lane_id}"
    bl_lock = f"bl:{body.bl_ref}"
    conn = connect()
    try:
        # 事务一（立即写事务）：占车道锁与提单锁。进入即拿库级 RESERVED 锁，
        # 双会话同提单号抢建在此串行；后到只插争用痕并随本事务正常提交留痕。
        # 注意：事务内不得抛异常，否则 write_tx 回滚会吞掉争用痕。
        with write_tx(conn):
            lane_ok = try_acquire(conn, lane_lock, user)
            bl_ok = try_acquire(conn, bl_lock, user) if lane_ok else False
            contended = not (lane_ok and bl_ok)
        if contended:
            raise HTTPException(409, "争用：后到只记争用，不写业务")

        # 事务二（立即写事务）：业务写。失败仅回滚本事务，锁与争用痕已独立落盘。
        try:
            with write_tx(conn):
                lane = conn.execute(
                    """SELECT l.*, b.max_gross_t, b.axle_limit FROM weigh_lanes l
                       JOIN weigh_bridges b ON b.id=l.bridge_id WHERE l.id=?""",
                    (body.lane_id,),
                ).fetchone()
                if not lane:
                    raise HTTPException(404, "车道不存在")
                validate_lane_open(dict(lane))
                axles = [a.model_dump() for a in body.axle_weights]
                validate_axle_count(axles, int(lane["axle_limit"]))
                gross, _peak, _spread = validate_axle_weights(axles)
                validate_gross_cap(gross, float(lane["max_gross_t"]))
                bl = conn.execute(
                    "SELECT * FROM bl_manifests WHERE bl_code=?", (body.bl_ref,)
                ).fetchone()
                if not bl:
                    raise _NoBlGate
                is_over, overweight = adjudicate_overload(
                    gross, float(lane["overload_threshold_t"])
                )
                code = body.ticket_code or _next_ticket_code(conn, body.lane_id)
                if conn.execute(
                    "SELECT id FROM weigh_tickets WHERE ticket_code=?", (code,)
                ).fetchone():
                    raise HTTPException(409, "票号已存在")
                now = now_iso()
                status = "旁路挂起" if is_over else "已裁定"
                cur = conn.execute(
                    """INSERT INTO weigh_tickets(ticket_code,lane_id,bl_ref,vehicle_plate,
                       axle_weights,gross_t,generation,status,adjudicated_at,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        code,
                        body.lane_id,
                        body.bl_ref,
                        body.vehicle_plate,
                        json.dumps(axles, ensure_ascii=False),
                        gross,
                        1,
                        status,
                        now if status == "已裁定" else None,
                        now,
                    ),
                )
                ticket_id = cur.lastrowid
                if is_over:
                    conn.execute(
                        """INSERT INTO overload_holds(ticket_id,hold_key,overweight_t,threshold_t,
                           status,opened_at,side_lane_note) VALUES (?,?,?,?,?,?,?)""",
                        (
                            ticket_id,
                            f"HOLD-{code}",
                            overweight,
                            float(lane["overload_threshold_t"]),
                            "开放",
                            now,
                            lane["side_lane_ref"] or "旁路待驳",
                        ),
                    )
                write_audit(
                    conn,
                    user,
                    "ticket_weigh",
                    code,
                    {
                        "ticket_id": ticket_id,
                        "gross_t": gross,
                        "status": status,
                        "overload": is_over,
                    },
                )
        except _NoBlGate:
            # 事务三：无提单禁建偏航独立留痕（业务事务已回滚，正式册零增）
            with write_tx(conn):
                write_audit(
                    conn,
                    user,
                    "无提单禁建偏航",
                    body.bl_ref,
                    {
                        "reason": "提单册无匹配行",
                        "ticket_code": body.ticket_code,
                        "lane_id": body.lane_id,
                    },
                )
            raise HTTPException(
                422, "无提单禁建：提单册无匹配行，正式册零增并拒收"
            )
    finally:
        # 按获取反序释放：先车道锁、后提单锁。败者不持提单锁，其 bl 释放为
        # 空操作，不会删掉胜者/预占者已落盘的争用痕。
        release(conn, lane_lock, user)
        release(conn, bl_lock, user)
        conn.close()
    return {
        "id": ticket_id,
        "ticket_code": code,
        "gross_t": gross,
        "status": status,
        "overload": is_over,
    }
