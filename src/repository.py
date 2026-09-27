from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import AUTO_STOP_SHIFTS, ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS area_stop_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_code TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    shifts TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual'
                        CHECK(source IN ('manual','auto')),
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','resumed')),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_area_stop_orders_area
                    ON area_stop_orders(area_code, status);
                CREATE TABLE IF NOT EXISTS area_resume_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stop_id INTEGER NOT NULL
                        REFERENCES area_stop_orders(id) ON DELETE CASCADE,
                    area_code TEXT NOT NULL,
                    note TEXT,
                    status TEXT NOT NULL
                        CHECK(status IN ('approved','rejected','voided')),
                    blockers TEXT NOT NULL DEFAULT '[]',
                    void_reason TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_area_resume_orders_area
                    ON area_resume_orders(area_code, status);
            """)
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
            if "area_code" not in columns:
                self.conn.execute("ALTER TABLE items ADD COLUMN area_code TEXT")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, area_code: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       area_code)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, area_code),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def _active_stop_row(self, area_code: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM area_stop_orders
               WHERE area_code=? AND status='active' ORDER BY id DESC LIMIT 1""",
            (area_code,),
        ).fetchone()

    def get_active_stop(self, area_code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._active_stop_row(area_code)
        return dict(row) if row is not None else None

    def get_stop_order(self, stop_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM area_stop_orders WHERE id=?", (stop_id,)).fetchone()
        if row is None:
            raise NotFoundError("停工单不存在")
        return dict(row)

    def create_stop_order(self, area_code: str, reason: str, shifts: str,
                          source: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            if self._active_stop_row(area_code) is not None:
                raise ConflictError("区域已处于停工状态")
            cur = self.conn.execute(
                """INSERT INTO area_stop_orders(area_code, reason, shifts, source, status,
                   version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (area_code, reason, shifts, source, 'active', 1, actor, now, now),
            )
            stop_id = int(cur.lastrowid)
        return self.get_stop_order(stop_id)

    def ensure_area_stopped(self, area_code: str, reason: str, shifts: str,
                            source: str, actor: str):
        with self._lock:
            existing = self._active_stop_row(area_code)
        if existing is not None:
            return dict(existing), False
        try:
            return self.create_stop_order(area_code, reason, shifts, source, actor), True
        except ConflictError:
            return self.get_active_stop(area_code), False

    def _area_blockers(self, area_code: str) -> List[str]:
        blockers: List[str] = []
        rows = self.conn.execute(
            """SELECT id, title, status FROM items
               WHERE area_code=? AND status<>'closed' ORDER BY id""",
            (area_code,),
        ).fetchall()
        for row in rows:
            blockers.append(
                f"未结事故{ID_PREFIX}-{row['id']:04d}：{row['title']}（状态：{row['status']}）")
        rows = self.conn.execute(
            """SELECT r.id AS rid, r.kind AS kind, r.item_id AS item_id
               FROM records r JOIN items i ON r.item_id=i.id
               WHERE i.area_code=? AND r.status='open' ORDER BY r.id""",
            (area_code,),
        ).fetchall()
        for row in rows:
            blockers.append(
                f"整改记录#{row['rid']}未关闭（事故{ID_PREFIX}-{row['item_id']:04d}，{row['kind']}）")
        return blockers

    def area_blockers(self, area_code: str) -> List[str]:
        with self._lock:
            return self._area_blockers(area_code)

    @staticmethod
    def _resume(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["blockers"] = json.loads(item["blockers"])
        return item

    def get_resume_order(self, resume_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM area_resume_orders WHERE id=?", (resume_id,)).fetchone()
        if row is None:
            raise NotFoundError("复工单不存在")
        return self._resume(row)

    def resume_area(self, area_code: str, note: Optional[str], actor: str):
        now = utc_now()
        with self._lock, self.conn:
            stop = self._active_stop_row(area_code)
            if stop is None:
                raise ConflictError("区域未处于停工状态")
            blockers = self._area_blockers(area_code)
            status = 'rejected' if blockers else 'approved'
            cur = self.conn.execute(
                """INSERT INTO area_resume_orders(stop_id, area_code, note, status, blockers,
                   version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (stop["id"], area_code, note, status,
                 json.dumps(blockers, ensure_ascii=False), 1, actor, now, now),
            )
            resume_id = int(cur.lastrowid)
            if not blockers:
                self.conn.execute(
                    """UPDATE area_stop_orders SET status='resumed',
                       version=version+1, updated_at=? WHERE id=?""",
                    (now, stop["id"]),
                )
        return self.get_resume_order(resume_id), blockers

    def void_area_resume(self, area_code: str, reason: str, actor: str):
        now = utc_now()
        with self._lock, self.conn:
            if self._active_stop_row(area_code) is not None:
                return None
            row = self.conn.execute(
                """SELECT * FROM area_resume_orders
                   WHERE area_code=? AND status='approved' ORDER BY id DESC LIMIT 1""",
                (area_code,),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                """UPDATE area_resume_orders SET status='voided', void_reason=?,
                   version=version+1, updated_at=? WHERE id=?""",
                (reason, now, row["id"]),
            )
            cur = self.conn.execute(
                """INSERT INTO area_stop_orders(area_code, reason, shifts, source, status,
                   version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (area_code, reason, AUTO_STOP_SHIFTS, 'auto', 'active', 1, actor, now, now),
            )
            stop_id = int(cur.lastrowid)
        return {"stop": self.get_stop_order(stop_id), "voided_resume_id": int(row["id"])}

    def list_stop_orders(self, area_code: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM area_stop_orders"
        params: tuple = ()
        if area_code:
            sql += " WHERE area_code=?"
            params = (area_code,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_resume_orders(self, area_code: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM area_resume_orders"
        params: tuple = ()
        if area_code:
            sql += " WHERE area_code=?"
            params = (area_code,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._resume(row) for row in rows]

    def area_summary(self, area_code: str) -> Dict[str, Any]:
        with self._lock:
            active = self._active_stop_row(area_code)
            row = self.conn.execute(
                """SELECT COUNT(*) AS n, MAX(updated_at) AS touched
                   FROM area_stop_orders WHERE area_code=?""",
                (area_code,),
            ).fetchone()
        summary: Dict[str, Any] = {
            "area_code": area_code, "stop_count": int(row["n"]),
            "updated_at": row["touched"],
        }
        if active is not None:
            summary.update(status="stopped", current_reason=active["reason"],
                           active_stop_id=active["id"])
        else:
            summary.update(status="resumed" if summary["stop_count"] else "normal",
                           current_reason=None, active_stop_id=None)
        return summary

    def list_area_summaries(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT area_code FROM area_stop_orders ORDER BY area_code"
            ).fetchall()
        return [self.area_summary(row["area_code"]) for row in rows]

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
