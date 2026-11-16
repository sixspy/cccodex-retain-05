import sqlite3

# 分区四种存储态——计数口径的唯一来源，页面卡片、报告落库、库内核验都走它。
STORAGE_STATES = ("在线", "已归档", "已销毁", "裁撤中")


class CountDriftError(RuntimeError):
    """对账数字与分区实际行数对不上（混数），拒绝落库。"""


def count_partition_states(conn: sqlite3.Connection) -> dict[str, int]:
    """唯一计数口径：一次 GROUP BY 拿到全部存储态行数，同一读点内完成。

    任何需要「在线/归档/销毁/裁撤中」数字的地方都必须调本函数，
    不允许在别处另写 SELECT COUNT 再拼，以免两个读点混数。
    """
    rows = conn.execute(
        "SELECT storage_state, COUNT(*) AS c FROM partitions GROUP BY storage_state"
    ).fetchall()
    found = {r["storage_state"]: int(r["c"]) for r in rows}
    return {state: found.get(state, 0) for state in STORAGE_STATES}


def count_pending_judgments(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM judgments "
            "WHERE status IN ('待执行','执行中')"
        ).fetchone()["c"]
    )


def find_mismatches(conn: sqlite3.Connection) -> list[dict]:
    """错位：裁决已完成，但实物分区存储态与裁决动作不符。"""
    rows = conn.execute(
        """
        SELECT j.id AS judgment_id, j.decided_action, j.status AS judgment_status,
               p.id AS partition_id, p.domain_code, p.biz_date, p.storage_state
        FROM judgments j
        JOIN partitions p ON p.id = j.partition_id
        WHERE j.status = '已完成'
          AND (
            (j.decided_action = '归档' AND p.storage_state != '已归档')
            OR (j.decided_action = '销毁' AND p.storage_state != '已销毁')
          )
        ORDER BY j.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _build_metrics(conn: sqlite3.Connection) -> dict:
    """在同一读点内取齐全部数字，并立刻做行数对齐校验。"""
    states = count_partition_states(conn)
    total = sum(states.values())

    # 对齐守卫第一道：四态之和必须等于分区表总行数。
    table_total = int(
        conn.execute("SELECT COUNT(*) AS c FROM partitions").fetchone()["c"]
    )
    if total != table_total:
        raise CountDriftError(
            f"计数口径不一致：存储态合计 {total} ≠ 分区总行数 {table_total}，已拦截落库"
        )

    pending = count_pending_judgments(conn)
    mismatches = find_mismatches(conn)
    return {
        "online": states["在线"],
        "archived": states["已归档"],
        "destroyed": states["已销毁"],
        "reconciling": states["裁撤中"],
        "partition_total": total,
        "pending": pending,
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
    }


def _assert_aligned(conn: sqlite3.Connection, m: dict) -> None:
    """对齐守卫第二道：落笔前在同一事务内重新取数复核，数字一旦漂移就拦。"""
    check = _build_metrics(conn)
    keys = (
        "online",
        "archived",
        "destroyed",
        "reconciling",
        "partition_total",
        "pending",
        "mismatch_count",
    )
    for key in keys:
        if check[key] != m[key]:
            raise CountDriftError(
                f"计数口径不一致：落笔复核 {key} 由 {m[key]} 漂移为 {check[key]}，已拦截落库"
            )


def run_reconcile(conn: sqlite3.Connection, as_of_date: str) -> dict:
    """策略视角与实物分区对账并落库。

    口径钉死：全部计数与错位检测在同一个写事务（BEGIN IMMEDIATE）的
    同一读点内完成，报告 INSERT 前再复核一次；任何混数都抛
    CountDriftError 回滚，库里不会出现账实不符的报告。
    """
    metrics = _build_metrics(conn)
    snapshot_at = conn.execute("SELECT datetime('now') AS t").fetchone()["t"]
    summary = (
        f"在线{metrics['online']} / 已归档{metrics['archived']} / "
        f"已销毁{metrics['destroyed']} / 裁撤中{metrics['reconciling']} / "
        f"分区合计{metrics['partition_total']} / "
        f"待执行裁决{metrics['pending']} / 错位{metrics['mismatch_count']}"
    )

    _assert_aligned(conn, metrics)

    cur = conn.execute(
        "INSERT INTO reconcile_reports "
        "(as_of_date, online_count, archived_count, destroyed_count, "
        "reconciling_count, partition_total, pending_judgment_count, "
        "mismatch_count, summary, snapshot_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            as_of_date,
            metrics["online"],
            metrics["archived"],
            metrics["destroyed"],
            metrics["reconciling"],
            metrics["partition_total"],
            metrics["pending"],
            metrics["mismatch_count"],
            summary,
            snapshot_at,
        ),
    )
    report_id = int(cur.lastrowid)

    # 落库后再读回核对：报告行数字必须与本事务分区行数一致，混数即拦。
    row = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "reconciling_count, partition_total, pending_judgment_count, "
        "mismatch_count, summary, snapshot_at, created_at "
        "FROM reconcile_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    report = dict(row)
    if (
        report["online_count"] != metrics["online"]
        or report["archived_count"] != metrics["archived"]
        or report["destroyed_count"] != metrics["destroyed"]
        or report["reconciling_count"] != metrics["reconciling"]
        or report["partition_total"] != metrics["partition_total"]
        or report["mismatch_count"] != metrics["mismatch_count"]
    ):
        raise CountDriftError("对账报告落库数字与分区行数对不上，已回滚拦截")
    report["mismatches"] = metrics["mismatches"]
    return report


def list_reports(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "reconciling_count, partition_total, pending_judgment_count, "
        "mismatch_count, summary, snapshot_at, created_at "
        "FROM reconcile_reports ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def build_snapshot(conn: sqlite3.Connection, snapshot_at: str) -> dict:
    """读页冻结快照：对账卡片、执行列表、分区详情三块同源同读点。

    只做只读查询，所有数字来自同一个事务快照，禁止各块自行开连接取瞬时数。
    """
    metrics = _build_metrics(conn)
    partitions = [
        dict(r)
        for r in conn.execute(
            "SELECT id, domain_code, biz_date, row_count, storage_state, "
            "archive_uri, created_at FROM partitions "
            "ORDER BY domain_code, biz_date"
        ).fetchall()
    ]
    runs = [
        dict(r)
        for r in conn.execute(
            "SELECT id, judgment_id, action, result_state, archive_uri, ok, "
            "detail, created_at FROM execute_runs ORDER BY id DESC"
        ).fetchall()
    ]
    latest = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "reconciling_count, partition_total, pending_judgment_count, "
        "mismatch_count, summary, snapshot_at, created_at "
        "FROM reconcile_reports ORDER BY id DESC LIMIT 1"
    ).fetchone()

    # 快照内再做一次对齐：卡片四态合计必须等于分区详情行数。
    aligned = metrics["partition_total"] == len(partitions)
    return {
        "snapshot_at": snapshot_at,
        "count_basis": "同一读事务冻结快照（在线/归档/销毁/裁撤中同源计数）",
        "counts": {
            "online": metrics["online"],
            "archived": metrics["archived"],
            "destroyed": metrics["destroyed"],
            "reconciling": metrics["reconciling"],
            "partition_total": metrics["partition_total"],
            "pending_judgment": metrics["pending"],
            "mismatch": metrics["mismatch_count"],
        },
        "count_aligned": aligned,
        "mismatches": metrics["mismatches"],
        "latest_report": dict(latest) if latest else None,
        "partitions": partitions,
        "execute_runs": runs,
    }
