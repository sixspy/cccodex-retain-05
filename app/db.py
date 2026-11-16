import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


class WriteLockedError(RuntimeError):
    """已有写事务在进行（另一名值班正在落笔），本次写入被互斥锁拒绝。"""


# 应用级写互斥：单进程内任何时刻只允许一个写事务（扫描 / 执行 / 对账落库共用）。
# SQLite 自身的库级锁兜底跨连接并发，这把锁负责在应用层快速失败而不是排队双写。
_write_guard = threading.Lock()

# 等锁上限：SQLite 层遇到 BUSY 时最多等 5 秒，而不是立刻抛错。
_BUSY_TIMEOUT_MS = 5000


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_transaction(db_path: str | None = None):
    """串行化一切落笔操作。

    非阻塞抢应用级互斥锁：抢不到说明另一名值班正在写，直接抛
    WriteLockedError（路由层翻译成 409），绝不排队后再双写。
    抢到后以 BEGIN IMMEDIATE 立刻拿 SQLite 写锁，整个事务内的读与写
    共用同一个读点，杜绝「先读旧数、后写新数」的口径混用。
    """
    if not _write_guard.acquire(blocking=False):
        raise WriteLockedError("已有写操作进行中，请稍后重试（同一时刻只允许一人落笔）")
    conn = connect(db_path)
    try:
        # isolation_level=None：事务边界完全由我们自己掌控
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # 跨进程/跨连接抢 SQLite 写锁失败（等满 busy_timeout）：
            # 统一翻译成应用层写锁冲突，路由层回 409 而不是裸 500。
            if "locked" in str(exc).lower():
                raise WriteLockedError(
                    "数据库写锁被占用，请稍后重试（同一时刻只允许一人落笔）"
                ) from exc
            raise
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
        _write_guard.release()


@contextmanager
def read_snapshot(db_path: str | None = None):
    """读页瞬间冻结快照：一个只读事务内的所有查询共享同一读点。

    对账卡片 / 执行列表 / 分区详情三块必须出自同一快照，禁止各自开连接
    取瞬时数。页面侧拿到的 snapshot_at 即冻结时刻。
    """
    conn = connect(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN")
        snapshot_at = conn.execute("SELECT datetime('now') AS t").fetchone()["t"]
        try:
            yield conn, snapshot_at
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS retain_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_code TEXT NOT NULL,
  domain_name TEXT NOT NULL,
  keep_days INTEGER NOT NULL,
  action_on_expire TEXT NOT NULL CHECK (action_on_expire IN ('归档','销毁')),
  version INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain_code, version)
);

CREATE TABLE IF NOT EXISTS partitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  row_count INTEGER NOT NULL DEFAULT 0,
  storage_state TEXT NOT NULL CHECK (
    storage_state IN ('在线','已归档','已销毁','裁撤中')
  ),
  archive_uri TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain_code, biz_date)
);

CREATE TABLE IF NOT EXISTS scan_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  status TEXT NOT NULL,
  expired_count INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS judgments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_job_id INTEGER NOT NULL,
  partition_id INTEGER NOT NULL,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  policy_version INTEGER NOT NULL,
  keep_days INTEGER NOT NULL,
  decided_action TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待执行','执行中','已完成','已失败','已作废')),
  fail_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(scan_job_id) REFERENCES scan_jobs(id),
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);

CREATE TABLE IF NOT EXISTS execute_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  judgment_id INTEGER NOT NULL UNIQUE,
  action TEXT NOT NULL,
  result_state TEXT NOT NULL,
  archive_uri TEXT,
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(judgment_id) REFERENCES judgments(id)
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  online_count INTEGER NOT NULL,
  archived_count INTEGER NOT NULL,
  destroyed_count INTEGER NOT NULL,
  reconciling_count INTEGER NOT NULL DEFAULT 0,
  partition_total INTEGER NOT NULL DEFAULT 0,
  pending_judgment_count INTEGER NOT NULL,
  mismatch_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  snapshot_at TEXT NOT NULL DEFAULT (datetime('now')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# 老库平滑加列（IF NOT EXISTS 不支持 ALTER TABLE，逐列探测后补）。
_MIGRATION_COLUMNS = {
    "reconcile_reports": [
        ("reconciling_count", "INTEGER NOT NULL DEFAULT 0"),
        ("partition_total", "INTEGER NOT NULL DEFAULT 0"),
        ("snapshot_at", "TEXT"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATION_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # 老行补快照时刻：没有就以落库时间为准
    conn.execute(
        "UPDATE reconcile_reports SET snapshot_at = created_at "
        "WHERE snapshot_at IS NULL OR snapshot_at = ''"
    )


SEED_POLICIES = [
    ("orders", "订单明细域", 7, "归档"),
    ("payments", "支付流水域", 14, "归档"),
    ("events", "埋点事件域", 3, "销毁"),
    ("inventory", "库存快照域", 10, "归档"),
]

SEED_PARTITIONS = [
    ("orders", "2026-11-01", 1200, "在线"),
    ("orders", "2026-11-05", 980, "在线"),
    ("orders", "2026-11-10", 1100, "在线"),
    ("orders", "2026-11-14", 1050, "在线"),
    ("payments", "2026-10-20", 800, "在线"),
    ("payments", "2026-11-01", 760, "在线"),
    ("payments", "2026-11-12", 790, "在线"),
    ("events", "2026-11-08", 5000, "在线"),
    ("events", "2026-11-12", 5200, "在线"),
    ("events", "2026-11-15", 5100, "在线"),
    ("inventory", "2026-10-30", 300, "在线"),
    ("inventory", "2026-11-11", 310, "在线"),
]


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM retain_policies").fetchone()["c"]
        if n == 0:
            for domain_code, domain_name, keep_days, action in SEED_POLICIES:
                conn.execute(
                    "INSERT INTO retain_policies "
                    "(domain_code, domain_name, keep_days, action_on_expire, version, active) "
                    "VALUES (?,?,?,?,1,1)",
                    (domain_code, domain_name, keep_days, action),
                )
            for domain_code, biz_date, row_count, state in SEED_PARTITIONS:
                conn.execute(
                    "INSERT INTO partitions "
                    "(domain_code, biz_date, row_count, storage_state) "
                    "VALUES (?,?,?,?)",
                    (domain_code, biz_date, row_count, state),
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('lab_name', ?)",
                (Config.LAB_NAME,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('skeleton', ?)",
                (
                    "分区留存策略版本→到期扫描裁决→归档或销毁执行→策略与实物分区对账",
                ),
            )
