from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_optional_int,
                     require_optional_text, require_text, require_text_list)
from .repository import Repository
from .rules import (AREA_ENTITY, AREA_RESUME_ENTITY, AREA_RESUME_ROLES,
                    AREA_STOP_ROLES, AREA_VIEW_ROLES, AUDIT_ROLES, CREATE_ROLES,
                    ENTITY, RECORD_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    default_stop_scope, escalation_required, normalize_area_scope,
                    priority_score, response_deadline_hours, resume_blockers,
                    role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ------------------------------------------------------------------
    # 事故与整改
    # ------------------------------------------------------------------
    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        area_code = payload.get("area_code")
        if area_code is not None:
            area_code = require_text(area_code, "area_code", 50)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor, area_code)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
            "area_code": area_code,
        })
        if area_code:
            self._auto_revert_area(
                area_code,
                f"区域内新发生事故#{item['id']}（{severity}）：{title}",
                actor, trigger_item_id=item["id"])
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        if status == "open":
            item = self.repository.get_item(item_id)
            if item.get("area_code"):
                self._auto_revert_area(
                    item["area_code"],
                    f"事故#{item_id}新增未关闭整改措施（{kind}，记录#{record['id']}）",
                    actor, trigger_item_id=item_id)
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result

    # ------------------------------------------------------------------
    # 区域停复工台账
    # ------------------------------------------------------------------
    def _area_blockers(self, order: Dict[str, Any]) -> List[str]:
        linked_open_records = 0
        linked_open = False
        if order.get("item_id"):
            linked = self.repository.get_item(order["item_id"])
            linked_open = linked["status"] != "closed"
            linked_open_records = self.repository.open_record_count(order["item_id"])
        open_items = self.repository.open_area_item_ids(order["area_code"])
        open_record_items = self.repository.area_open_record_item_ids(order["area_code"])
        return resume_blockers(linked_open, linked_open_records,
                               open_items, open_record_items)

    def _auto_revert_area(self, area_code: str, reason: str, actor: str,
                          trigger_item_id: Optional[int] = None) -> None:
        """复工后同区域再发事故或新增未关闭措施：原复工单作废，区域回到停工。"""
        latest = self.repository.get_latest_area_order(area_code)
        if latest is None or latest["status"] != "resumed":
            return
        voided = self.repository.void_active_resumption(latest["id"], reason, actor)
        order = self.repository.revert_area_order(latest["id"], reason, actor)
        detail: Dict[str, Any] = {"area_code": area_code, "reason": reason}
        if trigger_item_id is not None:
            detail["trigger_item_id"] = trigger_item_id
        if voided is not None:
            detail["resumption_id"] = voided["id"]
        self.repository.append_audit("area_auto_revert", AREA_ENTITY, order["id"],
                                     actor, detail)

    def register_area_stop(self, payload: Dict[str, Any], actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, AREA_STOP_ROLES)
        actor = require_text(actor, "actor", 100)
        area_code = require_text(payload.get("area_code"), "area_code", 50)
        reason = require_text(payload.get("reason"), "reason")
        shifts = require_text_list(payload.get("affected_shifts"), "affected_shifts")
        item_id = require_optional_int(payload.get("item_id"), "item_id")
        equipment_tag = require_optional_text(payload.get("equipment_tag"),
                                              "equipment_tag", 100)
        severity = None
        if item_id is not None:
            linked = self.repository.get_item(item_id)
            severity = linked["severity"]
            if linked.get("area_code") and linked["area_code"] != area_code:
                raise ValidationError("关联事故所属区域与登记区域不一致")
        requested_scope = payload.get("scope")
        if severity is not None:
            scope = normalize_area_scope(requested_scope, severity)
        else:
            scope = requested_scope or "whole_area"
            if scope not in ("equipment", "whole_area"):
                raise ValidationError("scope不在允许范围内")
        if scope == "equipment" and not equipment_tag:
            raise ValidationError("只停单台设备时必须填写设备/工位标识")
        latest = self.repository.get_latest_area_order(area_code)
        superseded_id = None
        if latest is not None:
            if latest["status"] == "stopped":
                raise ConflictError(
                    f"区域{area_code}当前已在停工（停工单#{latest['id']}）",
                    details={"blockers": [
                        f"区域仍在停工，当前原因：{latest['current_reason']}"],
                        "active_order_id": latest["id"]})
            if latest["status"] == "resumed":
                self.repository.void_active_resumption(
                    latest["id"], f"区域重新登记停工：{reason}", actor)
            self.repository.supersede_area_orders(area_code, actor)
            superseded_id = latest["id"]
        order = self.repository.create_area_order(
            area_code, reason, shifts, scope, equipment_tag, item_id, actor)
        audit_detail: Dict[str, Any] = {
            "area_code": area_code, "reason": reason, "affected_shifts": shifts,
            "scope": scope, "default_scope": default_stop_scope(severity)
            if severity else scope,
        }
        if item_id is not None:
            audit_detail["item_id"] = item_id
        if superseded_id is not None:
            audit_detail["superseded_order_id"] = superseded_id
        self.repository.append_audit("area_stop_register", AREA_ENTITY, order["id"],
                                     actor, audit_detail)
        return self.enrich_area_order(order)

    def resume_area(self, order_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, AREA_RESUME_ROLES)
        actor = require_text(actor, "actor", 100)
        order = self.repository.get_area_order(order_id)
        if order["status"] != "stopped":
            raise ConflictError("该停工单当前不在停工状态")
        expected_version = require_optional_int(
            payload.get("expected_version"), "expected_version")
        if expected_version is None:
            raise ValidationError("expected_version必须提交")
        note = require_optional_text(payload.get("note"), "note")
        blockers = self._area_blockers(order)
        if blockers:
            resumption = self.repository.add_area_resumption(
                order_id, "rejected", blockers, note, actor)
            self.repository.append_audit("area_resume_reject", AREA_RESUME_ENTITY,
                                         resumption["id"], actor, {
                                             "order_id": order_id,
                                             "blockers": blockers})
            raise ConflictError("复工被退回：" + "；".join(blockers),
                                details={"blockers": blockers,
                                         "resumption_id": resumption["id"]})
        updated = self.repository.mark_area_order_resumed(
            order_id, expected_version, actor)
        resumption = self.repository.add_area_resumption(
            order_id, "active", [], note, actor)
        self.repository.append_audit("area_resume_approve", AREA_RESUME_ENTITY,
                                     resumption["id"], actor, {
                                         "order_id": order_id,
                                         "resumption_id": resumption["id"]})
        return self.enrich_area_order(updated)

    def list_area_orders(self, role: str,
                         area_code: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_role(role, AREA_VIEW_ROLES)
        return [self.enrich_area_order(order)
                for order in self.repository.list_area_orders(area_code)]

    def get_area_order_detail(self, order_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, AREA_VIEW_ROLES)
        order = self.repository.get_area_order(order_id)
        result = self.enrich_area_order(order)
        result["resumptions"] = self.repository.list_area_resumptions(order_id)
        return result

    def enrich_area_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(order)
        result["blockers"] = (self._area_blockers(order)
                              if order["status"] == "stopped" else [])
        return result
