#!/usr/bin/env python3
"""落库扫描核验：绕过 Web 层直连本机 SQLite，验证对账报告与分区行数对齐。

用法：
  python scripts/verify_reconcile_db.py [--db instance/retain_lab.db]

退出码 0 表示全部报告账实相符；发现混数/脏报告时退出码 1。
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATES = ("在线", "已归档", "已销毁", "裁撤中")


def main() -> int:
    parser = argparse.ArgumentParser(description="对账报告落库核验")
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "DATABASE_PATH", str(ROOT / "instance" / "retain_lab.db")
        ),
        help="SQLite 库路径（默认读 DATABASE_PATH 或 instance/retain_lab.db）",
    )
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"[FAIL] 数据库不存在：{args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    problems: list[str] = []

    state_rows = conn.execute(
        "SELECT storage_state, COUNT(*) AS c FROM partitions GROUP BY storage_state"
    ).fetchall()
    live = {r["storage_state"]: int(r["c"]) for r in state_rows}
    live_total = sum(live.get(s, 0) for s in STATES)
    table_total = conn.execute("SELECT COUNT(*) AS c FROM partitions").fetchone()["c"]

    print(f"== 分区当前实况（{args.db}） ==")
    for s in STATES:
        print(f"  {s}: {live.get(s, 0)}")
    print(f"  四态合计: {live_total} / 分区表总行数: {table_total}")
    if live_total != table_total:
        problems.append(f"存储态合计 {live_total} ≠ 分区总行数 {table_total}")

    reports = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "reconciling_count, partition_total, mismatch_count, summary, snapshot_at "
        "FROM reconcile_reports ORDER BY id"
    ).fetchall()
    print(f"\n== 对账报告 {len(reports)} 份 ==")
    if not reports:
        print("  （无报告，先在页面生成一份）")

    for r in reports:
        tag = f"报告#{r['id']} @ {r['as_of_date']} (snapshot {r['snapshot_at']})"
        state_sum = (
            r["online_count"]
            + r["archived_count"]
            + r["destroyed_count"]
            + r["reconciling_count"]
        )
        if state_sum != r["partition_total"]:
            problems.append(
                f"{tag}：四态之和 {state_sum} ≠ partition_total {r['partition_total']}"
            )
            continue
        # 历史报告是冻结快照，只校验自洽（四态加总=合计、摘要含全部口径），
        # 不要求与「当前」分区数一致——那正是快照存在的意义。
        for key, label in (
            ("online_count", "在线"),
            ("archived_count", "已归档"),
            ("destroyed_count", "已销毁"),
            ("reconciling_count", "裁撤中"),
        ):
            if label not in (r["summary"] or ""):
                problems.append(f"{tag}：摘要缺少口径「{label}」")
        print(
            f"  {tag}: 在线{r['online_count']} 归档{r['archived_count']} "
            f"销毁{r['destroyed_count']} 裁撤中{r['reconciling_count']} "
            f"合计{r['partition_total']} 错位{r['mismatch_count']} [自洽]"
        )

    # 错位条数与实际 JOIN 结果核对
    actual_mismatch = conn.execute(
        """
        SELECT COUNT(*) AS c FROM judgments j
        JOIN partitions p ON p.id = j.partition_id
        WHERE j.status = '已完成'
          AND ((j.decided_action = '归档' AND p.storage_state != '已归档')
            OR (j.decided_action = '销毁' AND p.storage_state != '已销毁'))
        """
    ).fetchone()["c"]
    latest_mismatch = reports[-1]["mismatch_count"] if reports else None
    if latest_mismatch is not None and latest_mismatch != actual_mismatch:
        problems.append(
            f"最新报告错位 {latest_mismatch} ≠ 库内实查错位 {actual_mismatch}"
        )

    conn.close()

    print("\n== 核验结论 ==")
    if problems:
        for p in problems:
            print(f"  [FAIL] {p}")
        return 1
    print("  [PASS] 报告计数自洽，错位条数与库内实查一致，无混数脏报告。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
