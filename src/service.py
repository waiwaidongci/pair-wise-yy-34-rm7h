from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (NotFoundError, ResumeBlockedError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AREA_RESUME_ROLES, AREA_STOP_ROLES, AREA_VIEW_ROLES,
                    AUDIT_ROLES, AUTO_STOP_SHIFTS, CREATE_ROLES, ENTITY,
                    RECORD_ROLES, TITLE, VIEW_ROLES, auto_stop_required,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

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
            area_code = require_text(area_code, "area_code", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor, area_code)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        self._apply_accident_area_rules(item, actor)
        return self.enrich(item)

    def _apply_accident_area_rules(self, item: Dict[str, Any], actor: str) -> None:
        area_code = item.get("area_code")
        if not area_code or self.repository.get_active_stop(area_code) is not None:
            return
        severe = auto_stop_required(item["severity"])
        if severe:
            reason = f"严重事故默认停整片区域：{item['title']}"
        else:
            reason = f"复工后同区域再发生事故：{item['title']}"
        voided = self.repository.void_area_resume(area_code, reason, actor)
        if voided is not None:
            self.repository.append_audit("area_resume_void", "area_resume",
                                         voided["voided_resume_id"], actor, {
                "area_code": area_code, "reason": reason, "item_id": item["id"]})
        elif severe:
            stop, created = self.repository.ensure_area_stopped(
                area_code, reason, AUTO_STOP_SHIFTS, "auto", actor)
            if created:
                self.repository.append_audit("area_stop", "area_stop", stop["id"],
                                             actor, {
                    "area_code": area_code, "reason": reason, "source": "auto",
                    "item_id": item["id"]})

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
            area_code = item.get("area_code")
            if area_code:
                reason = f"复工后新增未关闭措施：{kind}"
                voided = self.repository.void_area_resume(area_code, reason, actor)
                if voided is not None:
                    self.repository.append_audit(
                        "area_resume_void", "area_resume",
                        voided["voided_resume_id"], actor, {
                            "area_code": area_code, "reason": reason,
                            "record_id": record["id"]})
        return record

    def stop_area(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, AREA_STOP_ROLES)
        actor = require_text(actor, "actor", 100)
        area_code = require_text(payload.get("area_code"), "area_code", 100)
        reason = require_text(payload.get("reason"), "reason")
        shifts = require_text(payload.get("shifts"), "shifts", 200)
        stop = self.repository.create_stop_order(area_code, reason, shifts,
                                                 "manual", actor)
        self.repository.append_audit("area_stop", "area_stop", stop["id"], actor, {
            "area_code": area_code, "reason": reason, "shifts": shifts,
            "source": "manual"})
        return stop

    def resume_area(self, area_code: str, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, AREA_RESUME_ROLES)
        actor = require_text(actor, "actor", 100)
        area_code = require_text(area_code, "area_code", 100)
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note")
        order, blockers = self.repository.resume_area(area_code, note, actor)
        if blockers:
            self.repository.append_audit("area_resume_rejected", "area_resume",
                                         order["id"], actor, {
                "area_code": area_code, "blockers": blockers})
            raise ResumeBlockedError(blockers)
        self.repository.append_audit("area_resume", "area_resume", order["id"],
                                     actor, {
            "area_code": area_code, "stop_id": order["stop_id"]})
        return order

    def get_area(self, area_code: str, role: str) -> Dict[str, Any]:
        ensure_role(role, AREA_VIEW_ROLES)
        area_code = require_text(area_code, "area_code", 100)
        summary = self.repository.area_summary(area_code)
        if summary["stop_count"] == 0:
            raise NotFoundError("区域无停复工记录")
        summary["stop_orders"] = self.repository.list_stop_orders(area_code)
        summary["resume_orders"] = self.repository.list_resume_orders(area_code)
        summary["blockers"] = (self.repository.area_blockers(area_code)
                               if summary["status"] == "stopped" else [])
        return summary

    def list_areas(self, role: str) -> list:
        ensure_role(role, AREA_VIEW_ROLES)
        return self.repository.list_area_summaries()

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
            from .domain import ConflictError
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
