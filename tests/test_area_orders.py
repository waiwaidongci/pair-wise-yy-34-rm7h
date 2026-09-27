import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import STATES, TRANSITION_ROLES
from src.service import Service


class AreaOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _create_item(self, severity="serious", area="A1", ref=None,
                     title="injury in area"):
        payload = {"title": title, "description": "hurt on site",
                   "severity": severity, "quantity": 2, "threshold": 10}
        if area:
            payload["area_code"] = area
        if ref:
            payload["external_ref"] = ref
        return self.service.create_item(payload, "reporter", "reporter")

    def _close_item(self, item):
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "u",
                TRANSITION_ROLES[target][0])
        return current

    def _stop(self, area="A1", item=None, reason="机器夹手", role="supervisor",
              scope=None, shifts=("早班",), equipment_tag=None, extra=None):
        payload = {"area_code": area, "reason": reason,
                   "affected_shifts": list(shifts)}
        if item is not None:
            payload["item_id"] = item["id"]
        if scope is not None:
            payload["scope"] = scope
        if equipment_tag is not None:
            payload["equipment_tag"] = equipment_tag
        if extra:
            payload.update(extra)
        return self.service.register_area_stop(payload, "foreman", role)

    def test_serious_accident_defaults_to_whole_area_and_rejects_equipment(self):
        item = self._create_item(severity="serious")
        order = self._stop(item=item)
        self.assertEqual(order["scope"], "whole_area")
        payload = {"area_code": "A1", "reason": "x", "affected_shifts": ["夜班"],
                   "item_id": item["id"], "scope": "equipment",
                   "equipment_tag": "P-9"}
        with self.assertRaises(ValidationError):
            self.service.register_area_stop(payload, "foreman", "supervisor")
        # fatal 同样强制整片区域
        fatal = self._create_item(severity="fatal", area="A3")
        order3 = self._stop(area="A3", item=fatal)
        self.assertEqual(order3["scope"], "whole_area")

    def test_equipment_scope_requires_tag(self):
        item = self._create_item(severity="minor", area="B1")
        with self.assertRaises(ValidationError):
            self._stop(area="B1", item=item, scope="equipment")
        order = self._stop(area="B1", item=item, scope="equipment",
                           equipment_tag="CNC-07")
        self.assertEqual(order["scope"], "equipment")
        self.assertEqual(order["equipment_tag"], "CNC-07")

    def test_stop_requires_area_reason_and_shifts(self):
        for bad in ({}, {"area_code": "A1"}, {"area_code": "A1", "reason": "x"},
                    {"area_code": "A1", "reason": "x", "affected_shifts": []}):
            with self.assertRaises(ValidationError):
                self.service.register_area_stop(bad, "foreman", "supervisor")

    def test_only_supervisor_or_safety_manager_can_stop(self):
        with self.assertRaises(PermissionDenied):
            self._stop(role="reporter")
        with self.assertRaises(PermissionDenied):
            self._stop(role="viewer")

    def test_only_safety_manager_can_resume(self):
        order = self._stop(area="C1")
        body = {"expected_version": order["version"]}
        with self.assertRaises(PermissionDenied):
            self.service.resume_area(order["id"], body, "foreman", "supervisor")
        with self.assertRaises(PermissionDenied):
            self.service.resume_area(order["id"], body, "p", "investigator")

    def test_resume_blocked_by_open_linked_accident_and_records(self):
        item = self._create_item(severity="minor", area="D1")
        order = self._stop(area="D1", item=item, scope="equipment",
                           equipment_tag="P-1")
        with self.assertRaises(ConflictError) as caught:
            self.service.resume_area(
                order["id"], {"expected_version": order["version"]},
                "sm", "safety_manager")
        self.assertIn("区域内仍有未结事故", caught.exception.message)
        self.assertTrue(caught.exception.details["blockers"])
        # 停工单状态不变，退回单落库
        stopped = self.service.get_area_order_detail(order["id"], "viewer")
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["resumptions"][0]["status"], "rejected")
        self.assertTrue(stopped["resumptions"][0]["blockers"])
        # 关闭事故后复工成功（minor 事故无 open 记录即可关闭）
        self._close_item(item)
        order = self.service.get_area_order_detail(order["id"], "viewer")
        result = self.service.resume_area(
            order["id"], {"expected_version": order["version"]},
            "sm", "safety_manager")
        self.assertEqual(result["status"], "resumed")
        detail = self.service.get_area_order_detail(order["id"], "viewer")
        self.assertEqual(detail["resumptions"][-1]["status"], "active")

    def test_resume_blocked_by_other_open_accident_and_open_measure(self):
        item = self._create_item(severity="minor", area="E1")
        self._close_item(item)
        order = self._stop(area="E1", item=item, scope="equipment",
                           equipment_tag="P-2")
        # 同区域另一起未结事故
        other = self._create_item(severity="minor", area="E1", title="second")
        with self.assertRaises(ConflictError) as caught:
            self.service.resume_area(
                order["id"], {"expected_version": order["version"]},
                "sm", "safety_manager")
        messages = "；".join(caught.exception.details["blockers"])
        self.assertIn("#%d" % other["id"], messages)
        # 事故登记一条未关闭整改，事故本身关闭后措施仍然阻断复工
        self.service.add_record(other["id"],
                                {"kind": "action", "detail": "add guard"},
                                "inv", "investigator")
        self.repo.conn.execute(
            "UPDATE items SET status='closed' WHERE id=?", (other["id"],))
        order = self.service.get_area_order_detail(order["id"], "viewer")
        with self.assertRaises(ConflictError) as caught:
            self.service.resume_area(
                order["id"], {"expected_version": order["version"]},
                "sm", "safety_manager")
        messages = "；".join(caught.exception.details["blockers"])
        self.assertIn("未关闭整改措施", messages)
        self.assertIn("#%d" % other["id"], messages)

    def test_new_accident_after_resume_voids_order_and_returns_to_stop(self):
        item = self._create_item(severity="minor", area="F1")
        self._close_item(item)
        order = self._stop(area="F1", item=item, scope="equipment",
                           equipment_tag="P-3")
        self.service.resume_area(order["id"],
                                 {"expected_version": order["version"]},
                                 "sm", "safety_manager")
        # 复工后同区域再发生事故
        second = self._create_item(severity="moderate", area="F1",
                                   title="recurrence")
        detail = self.service.get_area_order_detail(order["id"], "viewer")
        self.assertEqual(detail["status"], "stopped")
        self.assertIn(str(second["id"]), detail["current_reason"])
        self.assertEqual(detail["resumptions"][-1]["status"], "void")
        self.assertIn(str(second["id"]),
                      detail["resumptions"][-1]["void_reason"])
        # 列表也标出当前原因
        rows = self.service.list_area_orders("viewer", area_code="F1")
        self.assertIn(str(second["id"]), rows[0]["current_reason"])
        # 此时安全员提交复工应被退回
        with self.assertRaises(ConflictError):
            self.service.resume_area(
                order["id"], {"expected_version": detail["version"]},
                "sm", "safety_manager")

    def test_new_open_measure_after_resume_reverts_area(self):
        item = self._create_item(severity="minor", area="G1")
        self._close_item(item)
        order = self._stop(area="G1", item=item, scope="equipment",
                           equipment_tag="P-4")
        self.service.resume_area(order["id"],
                                 {"expected_version": order["version"]},
                                 "sm", "safety_manager")
        # 复工后对同区域已存在的事故追加未关闭整改措施
        self.service.add_record(item["id"],
                                {"kind": "action", "detail": "lockout fix",
                                 "status": "open"}, "inv", "investigator")
        detail = self.service.get_area_order_detail(order["id"], "viewer")
        self.assertEqual(detail["status"], "stopped")
        self.assertIn("未关闭整改措施", detail["current_reason"])
        self.assertEqual(detail["resumptions"][-1]["status"], "void")

    def test_double_stop_conflicts_and_re_register_supersedes(self):
        self._stop(area="H1")
        with self.assertRaises(ConflictError) as caught:
            self._stop(area="H1", reason="再次停工")
        self.assertIn("当前原因", "；".join(
            caught.exception.details["blockers"]))
        # 复工后重新登记停工：原复工单作废、原停工单 superseded
        first = self.service.list_area_orders("viewer", area_code="H1")[0]
        self.service.resume_area(first["id"],
                                 {"expected_version": first["version"]},
                                 "sm", "safety_manager")
        second = self._stop(area="H1", reason="新隐患停工")
        self.assertEqual(second["status"], "stopped")
        rows = self.service.list_area_orders("viewer", area_code="H1")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["status"] for r in rows}, {"superseded", "stopped"})
        old = next(r for r in rows if r["id"] != second["id"])
        self.assertTrue(any(
            r["status"] == "void" for r in
            self.service.get_area_order_detail(old["id"], "viewer")["resumptions"]))

    def test_audit_chain_intact(self):
        item = self._create_item(severity="minor", area="I1")
        order = self._stop(area="I1", item=item, scope="equipment",
                           equipment_tag="P-5")
        # 未结事故：复工退回
        with self.assertRaises(ConflictError):
            self.service.resume_area(order["id"],
                                     {"expected_version": order["version"]},
                                     "sm", "safety_manager")
        self._close_item(item)
        order = self.repo.get_area_order(order["id"])
        self.service.resume_area(order["id"],
                                 {"expected_version": order["version"]},
                                 "sm", "safety_manager")
        self._create_item(severity="minor", area="I1", title="again")
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
