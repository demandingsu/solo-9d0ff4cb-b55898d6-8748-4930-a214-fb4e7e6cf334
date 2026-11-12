"""SQLite 报价快照仓库：保存完整快照、计算字段差异。

一致性与并发：
- 每个来源一份“当前完整快照”与一条历史提交记录；对比与提交在同一个
  ``BEGIN IMMEDIATE`` 事务内完成，SQLite 写锁保证同一报价源并发采集时
  只有一个请求能基于最新已提交快照做比较并提交，另一个请求随后基于新快照比较。
- 只有完整采集成功才提交；任何问题地址只记录本次采集失败，原快照保持不变，
  因此不会把解析/抓取失败误报为“下架”。
- 比较的是快照内容（套餐字段与详情地址），重复采集相同内容不产生变更记录。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = os.environ.get("SNAPSHOT_DB_PATH", "./data/quote_snapshots.sqlite3")

COMPARED_FIELDS = ("supplier", "regions", "base_fee_cny", "peak_tiers", "valley_tiers")
# 内部字段不进入对外响应。
_INTERNAL_PLAN_FIELDS = ("detail_ref", "list_url")


def _public_plan(plan: dict | None) -> dict | None:
    if plan is None:
        return None
    return {key: value for key, value in plan.items() if key not in _INTERNAL_PLAN_FIELDS}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key    TEXT NOT NULL,
    list_url      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    plan_count    INTEGER NOT NULL,
    plans_json    TEXT NOT NULL,
    visited_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_source ON snapshots(source_key, id DESC);

CREATE TABLE IF NOT EXISTS snapshot_state (
    source_key       TEXT PRIMARY KEY,
    current_snapshot INTEGER NOT NULL REFERENCES snapshots(id),
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key  TEXT NOT NULL,
    list_url    TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    issues_json TEXT NOT NULL
);
"""


@dataclass
class SnapshotRecord:
    snapshot_id: int
    source_key: str
    list_url: str
    created_at: str
    plans: list[dict]  # 每项含 plan_id/... 与 detail_url
    visited_list_urls: list[str]


@dataclass
class FieldChange:
    plan_id: str
    plan_name: str | None
    change_type: str  # added | removed | changed
    field: str | None  # changed 时为字段名（含 detail_url）
    old_value: object
    new_value: object
    old_detail_url: str | None
    new_detail_url: str | None


@dataclass
class CommitOutcome:
    source_key: str
    status: str  # baseline_created | unchanged | changed
    snapshot_id: int
    previous_snapshot_id: int | None
    plan_count: int
    changes: list[FieldChange]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_write_transaction(conn: sqlite3.Connection, operation):
    """在 BEGIN IMMEDIATE 事务中执行 operation；遇到锁忙时有限重试。

    SQLite 写锁在 timeout 后报 ``database is locked``。多进程部署（多个 API
    实例共享同一数据库文件）时，短暂重试可以保证对比与提交不会以 5xx 中断。
    """
    delays = (0.05, 0.1, 0.2, 0.4)
    for attempt in range(len(delays) + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if attempt == len(delays) or "locked" not in str(exc).lower():
                raise
            time.sleep(delays[attempt])
            continue
        try:
            result = operation()
            conn.execute("COMMIT")
            return result
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    raise RuntimeError("无法获取 SQLite 写事务")


def _dump(value: object) -> object:
    """只保留可 JSON 序列化的规范化值。"""
    if isinstance(value, float) and value != value:  # NaN
        return None
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    return value


class SnapshotStore:
    def __init__(self, db_path: str | os.PathLike[str] | None = None):
        self.db_path = str(db_path or DEFAULT_DB_PATH)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # 同一进程内串行化写事务
        self._conn: sqlite3.Connection | None = None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def connection(self) -> sqlite3.Connection:
        # 单连接 + 进程内锁；WAL 供其它进程/线程只读。
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _init_schema(self) -> None:
        with self._lock:
            self.connection.executescript(_SCHEMA)

    def _current_row(self, conn: sqlite3.Connection, source_key: str) -> sqlite3.Row | None:
        row = conn.execute(
            "SELECT current_snapshot FROM snapshot_state WHERE source_key = ?", (source_key,)
        ).fetchone()
        if row is None:
            return None
        return conn.execute(
            "SELECT * FROM snapshots WHERE id = ?", (row["current_snapshot"],)
        ).fetchone()

    def current_snapshot(self, source_key: str) -> SnapshotRecord | None:
        """读取某来源当前快照（用于展示上一次基线，不参与提交事务）。"""
        with self._lock:
            row = self.connection.execute(
                "SELECT s.* FROM snapshot_state st JOIN snapshots s ON s.id = st.current_snapshot "
                "WHERE st.source_key = ?",
                (source_key,),
            ).fetchone()
        if row is None:
            return None
        return SnapshotRecord(
            snapshot_id=row["id"],
            source_key=row["source_key"],
            list_url=row["list_url"],
            created_at=row["created_at"],
            plans=json.loads(row["plans_json"]),
            visited_list_urls=json.loads(row["visited_json"]),
        )

    def record_failed_attempt(self, source_key: str, list_url: str, issues: list[dict]) -> None:
        payload = json.dumps(_dump(issues), ensure_ascii=False)
        with self._lock:
            conn = self.connection

            def operation() -> None:
                conn.execute(
                    "INSERT INTO crawl_attempts(source_key, list_url, attempted_at, ok, issues_json) "
                    "VALUES (?, ?, ?, 0, ?)",
                    (source_key, list_url, _utc_now(), payload),
                )

            _run_write_transaction(conn, operation)

    def commit_snapshot(self, source_key: str, list_url: str, plans: list[dict],
                        visited_list_urls: list[str]) -> CommitOutcome:
        """原子地比较并提交完整快照。

        在同一写事务内读取当前快照并计算差异；内容完全一致时不写新快照（unchanged）。
        """
        payload = json.dumps(_dump(plans), ensure_ascii=False, sort_keys=True)
        with self._lock:
            conn = self.connection

            def operation() -> CommitOutcome:
                previous_row = self._current_row(conn, source_key)
                if previous_row is None:
                    snapshot_id = self._insert(
                        conn, source_key, list_url, plans, payload, visited_list_urls
                    )
                    self._set_state(conn, source_key, snapshot_id)
                    return CommitOutcome(source_key, "baseline_created", snapshot_id, None,
                                         len(plans), [])

                previous = SnapshotRecord(
                    snapshot_id=previous_row["id"],
                    source_key=previous_row["source_key"],
                    list_url=previous_row["list_url"],
                    created_at=previous_row["created_at"],
                    plans=json.loads(previous_row["plans_json"]),
                    visited_list_urls=json.loads(previous_row["visited_json"]),
                )
                if previous_row["plans_json"] == payload:
                    return CommitOutcome(source_key, "unchanged", previous.snapshot_id,
                                         previous.snapshot_id, len(plans), [])

                changes = diff_snapshots(previous.plans, plans)
                snapshot_id = self._insert(conn, source_key, list_url, plans, payload,
                                           visited_list_urls)
                self._set_state(conn, source_key, snapshot_id)
                return CommitOutcome(source_key, "changed", snapshot_id, previous.snapshot_id,
                                     len(plans), changes)

            return _run_write_transaction(conn, operation)

    @staticmethod
    def _insert(conn: sqlite3.Connection, source_key: str, list_url: str, plans: list[dict],
                payload: str, visited_list_urls: list[str]) -> int:
        cursor = conn.execute(
            "INSERT INTO snapshots(source_key, list_url, created_at, plan_count, plans_json, visited_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source_key, list_url, _utc_now(), len(plans), payload,
             json.dumps(_dump(visited_list_urls), ensure_ascii=False)),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _set_state(conn: sqlite3.Connection, source_key: str, snapshot_id: int) -> None:
        conn.execute(
            "INSERT INTO snapshot_state(source_key, current_snapshot, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(source_key) DO UPDATE SET current_snapshot = excluded.current_snapshot, "
            "updated_at = excluded.updated_at",
            (source_key, snapshot_id, _utc_now()),
        )


def _canonical(value: object) -> object:
    """把字段值规范化为可直接比较的形式（JSON 往返保证类型一致）。"""
    return json.loads(json.dumps(_dump(value), ensure_ascii=False, sort_keys=True))


def diff_snapshots(old_plans: list[dict], new_plans: list[dict]) -> list[FieldChange]:
    """按套餐编号比较两份快照，返回上架 / 下架 / 改价与其它字段变化。

    每个变化都带新旧详情地址；新增与下架各生成一条记录。
    """
    old_by_id = {plan["plan_id"]: plan for plan in old_plans}
    new_by_id = {plan["plan_id"]: plan for plan in new_plans}
    changes: list[FieldChange] = []

    for plan_id in sorted(new_by_id.keys() - old_by_id.keys()):
        plan = new_by_id[plan_id]
        changes.append(
            FieldChange(plan_id, plan.get("name"), "added", None, None, _canonical(_public_plan(plan)),
                        None, plan.get("detail_url"))
        )
    for plan_id in sorted(old_by_id.keys() - new_by_id.keys()):
        plan = old_by_id[plan_id]
        changes.append(
            FieldChange(plan_id, plan.get("name"), "removed", None, _canonical(_public_plan(plan)), None,
                        plan.get("detail_url"), None)
        )

    for plan_id in sorted(old_by_id.keys() & new_by_id.keys()):
        old_plan, new_plan = old_by_id[plan_id], new_by_id[plan_id]
        old_detail, new_detail = old_plan.get("detail_url"), new_plan.get("detail_url")
        for field in COMPARED_FIELDS:
            old_value, new_value = old_plan.get(field), new_plan.get(field)
            if _canonical(old_value) != _canonical(new_value):
                changes.append(
                    FieldChange(plan_id, new_plan.get("name"), "changed", field,
                                _canonical(old_value), _canonical(new_value),
                                old_detail, new_detail)
                )
        if (old_plan.get("detail_ref") or "") != (new_plan.get("detail_ref") or ""):
            changes.append(
                FieldChange(plan_id, new_plan.get("name"), "changed", "detail_url",
                            old_detail, new_detail, old_detail, new_detail)
            )
    return changes
