from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (AREA_ORDER_STATES, AREA_RESUME_STATES, AREA_SCOPES, ID_PREFIX,
                    STATES)


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
        area_states = ",".join("'" + s + "'" for s in AREA_ORDER_STATES)
        resume_states = ",".join("'" + s + "'" for s in AREA_RESUME_STATES)
        scope_values = ",".join("'" + s + "'" for s in AREA_SCOPES)
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
                    area_code TEXT,
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
                CREATE INDEX IF NOT EXISTS ix_records_item_status ON records(item_id, status);
                CREATE TABLE IF NOT EXISTS area_stop_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_code TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    affected_shifts TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK(scope IN ({scope_values})),
                    equipment_tag TEXT,
                    item_id INTEGER REFERENCES items(id),
                    status TEXT NOT NULL CHECK(status IN ({area_states})),
                    current_reason TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_area_orders_area
                    ON area_stop_orders(area_code, id);
                CREATE TABLE IF NOT EXISTS area_resumptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL
                        REFERENCES area_stop_orders(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ({resume_states})),
                    blockers TEXT NOT NULL DEFAULT '[]',
                    note TEXT,
                    submitted_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    voided_at TEXT,
                    void_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_area_resumptions_order
                    ON area_resumptions(order_id, id);
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
            """)
            cols = {row["name"] for row in self.conn.execute(
                "PRAGMA table_info(items)").fetchall()}
            if "area_code" not in cols:
                self.conn.execute("ALTER TABLE items ADD COLUMN area_code TEXT")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_items_area ON items(area_code)")

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
                       status, version, external_ref, area_code, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, area_code, actor, now, now),
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

    def create_area_order(self, area_code: str, reason: str, shifts: List[str],
                          scope: str, equipment_tag: Optional[str],
                          item_id: Optional[int], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO area_stop_orders(area_code, reason, affected_shifts, scope,
                   equipment_tag, item_id, status, current_reason, version,
                   created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?, 'stopped', ?, 1, ?, ?, ?)""",
                (area_code, reason, json.dumps(shifts, ensure_ascii=False), scope,
                 equipment_tag, item_id, reason, actor, now, now),
            )
            order_id = int(cur.lastrowid)
        return self.get_area_order(order_id)

    @staticmethod
    def _area_order(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["affected_shifts"] = json.loads(result["affected_shifts"])
        return result

    @staticmethod
    def _resumption(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["blockers"] = json.loads(result["blockers"])
        return result

    def get_area_order(self, order_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM area_stop_orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise NotFoundError("区域停工单不存在")
        return self._area_order(row)

    def list_area_orders(self, area_code: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM area_stop_orders"
        params: tuple = ()
        if area_code:
            sql += " WHERE area_code=?"
            params = (area_code,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._area_order(row) for row in rows]

    def get_latest_area_order(self, area_code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM area_stop_orders WHERE area_code=? ORDER BY id DESC LIMIT 1",
                (area_code,)).fetchone()
        return self._area_order(row) if row else None

    def get_active_area_order(self, area_code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM area_stop_orders WHERE area_code=? AND status='stopped'
                   ORDER BY id DESC LIMIT 1""",
                (area_code,)).fetchone()
        return self._area_order(row) if row else None

    def supersede_area_orders(self, area_code: str, actor: str) -> int:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE area_stop_orders SET status='superseded', updated_at=?
                   WHERE area_code=? AND status IN ('resumed','superseded')""",
                (now, area_code))
        return cur.rowcount

    def mark_area_order_resumed(self, order_id: int, expected_version: int,
                                actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE area_stop_orders SET status='resumed', version=version+1,
                   updated_at=? WHERE id=? AND version=? AND status='stopped'""",
                (now, order_id, expected_version))
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM area_stop_orders WHERE id=?", (order_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("区域停工单不存在")
                row = self.conn.execute(
                    "SELECT status, version FROM area_stop_orders WHERE id=?",
                    (order_id,)).fetchone()
                if row["status"] != "stopped":
                    raise ConflictError("该停工单当前不在停工状态")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_area_order(order_id)

    def revert_area_order(self, order_id: int, reason: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE area_stop_orders SET status='stopped', version=version+1,
                   current_reason=?, updated_at=?
                   WHERE id=? AND status='resumed'""",
                (reason, now, order_id))
            if cur.rowcount == 0:
                raise ConflictError("该复工单已无法作废")
        return self.get_area_order(order_id)

    def add_area_resumption(self, order_id: int, status: str, blockers: List[str],
                            note: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO area_resumptions(order_id, status, blockers, note,
                   submitted_by, reviewed_by, created_at, voided_at, void_reason)
                   VALUES(?,?,?,?,?, NULL, ?, NULL, NULL)""",
                (order_id, status, json.dumps(blockers, ensure_ascii=False), note,
                 actor, now),
            )
            resumption_id = int(cur.lastrowid)
        return self.get_area_resumption(resumption_id)

    def get_area_resumption(self, resumption_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM area_resumptions WHERE id=?", (resumption_id,)).fetchone()
        if row is None:
            raise NotFoundError("区域复工单不存在")
        return self._resumption(row)

    def list_area_resumptions(self, order_id: Optional[int] = None,
                              status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM area_resumptions WHERE 1=1"
        params: list = []
        if order_id is not None:
            sql += " AND order_id=?"
            params.append(order_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [self._resumption(row) for row in rows]

    def get_active_resumption(self, order_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM area_resumptions WHERE order_id=? AND status='active'
                   ORDER BY id DESC LIMIT 1""", (order_id,)).fetchone()
        return self._resumption(row) if row else None

    def void_active_resumption(self, order_id: int, reason: str,
                               actor: str) -> Optional[Dict[str, Any]]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE area_resumptions SET status='void', voided_at=?, void_reason=?
                   WHERE order_id=? AND status='active'""",
                (now, reason, order_id))
            if cur.rowcount == 0:
                return None
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM area_resumptions WHERE order_id=? AND status='void'
                   ORDER BY id DESC LIMIT 1""", (order_id,)).fetchone()
        return self._resumption(row) if row else None

    def open_area_item_ids(self, area_code: str) -> List[int]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT id FROM items WHERE area_code=? AND status!='closed'
                   ORDER BY id""", (area_code,)).fetchall()
        return [int(r["id"]) for r in rows]

    def area_open_record_item_ids(self, area_code: str) -> List[int]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT DISTINCT i.id FROM items i
                   JOIN records r ON r.item_id=i.id
                   WHERE i.area_code=? AND r.status='open' ORDER BY i.id""",
                (area_code,)).fetchall()
        return [int(r["id"]) for r in rows]

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
