import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, NotFoundError, PermissionDenied, ResumeBlockedError, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class AreaLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _item(self,area="A-1",severity="minor",title="accident"):
        return self.service.create_item({"title":title,"description":"d","severity":severity,"quantity":1,"threshold":10,"area_code":area},"creator","reporter")
    def _close(self,item):
        current=item
        for target in STATES[1:]: current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        return current
    def test_manual_stop_permissions_and_duplicate(self):
        with self.assertRaises(PermissionDenied): self.service.stop_area({"area_code":"A-1","reason":"r","shifts":"早班"},"lead","viewer")
        with self.assertRaises(ValidationError): self.service.stop_area({"area_code":"A-1","reason":"","shifts":"早班"},"lead","site_supervisor")
        stop=self.service.stop_area({"area_code":"A-1","reason":"机械防护缺失","shifts":"早班"},"lead","site_supervisor")
        self.assertEqual(stop["status"],"active"); self.assertEqual(stop["source"],"manual")
        with self.assertRaises(ConflictError): self.service.stop_area({"area_code":"A-1","reason":"重复登记","shifts":"中班"},"lead","site_supervisor")
        area=self.service.get_area("A-1","viewer")
        self.assertEqual(area["status"],"stopped"); self.assertEqual(area["current_reason"],"机械防护缺失")
        self.assertIn("A-1",[a["area_code"] for a in self.service.list_areas("viewer")])
        with self.assertRaises(NotFoundError): self.service.get_area("NOPE","viewer")
    def test_severe_accident_auto_stops_area(self):
        self._item(area="B-1",severity="serious",title="冲压机伤害")
        area=self.service.get_area("B-1","viewer")
        self.assertEqual(area["status"],"stopped"); self.assertIn("严重事故默认停整片区域",area["current_reason"])
        self.assertEqual(area["stop_orders"][0]["shifts"],"全部班次"); self.assertEqual(area["stop_orders"][0]["source"],"auto")
        self._item(area="B-2",severity="minor")
        self.assertNotIn("B-2",[a["area_code"] for a in self.service.list_areas("viewer")])
    def test_resume_blocked_by_open_record_and_open_accident(self):
        item=self._item(area="C-1",severity="serious")
        self.service.add_record(item["id"],{"kind":"corrective","detail":"加装防护","status":"open"},"recorder","investigator")
        with self.assertRaises(PermissionDenied): self.service.resume_area("C-1",{},"lead","site_supervisor")
        with self.assertRaises(ResumeBlockedError) as ctx: self.service.resume_area("C-1",{"note":"申请复工"},"safety","safety_manager")
        blockers=ctx.exception.blockers
        self.assertTrue(any("未结事故" in b for b in blockers)); self.assertTrue(any("整改记录" in b for b in blockers))
        area=self.service.get_area("C-1","viewer")
        rejected=[o for o in area["resume_orders"] if o["status"]=="rejected"]
        self.assertEqual(len(rejected),1); self.assertEqual(rejected[0]["blockers"],blockers)
        self.assertEqual(area["status"],"stopped"); self.assertEqual(sorted(area["blockers"]),sorted(blockers))
    def test_resume_success_after_all_closed(self):
        item=self._item(area="D-1",severity="fatal")
        self._close(item)
        order=self.service.resume_area("D-1",{"note":"整改验收通过"},"safety","safety_manager")
        self.assertEqual(order["status"],"approved")
        area=self.service.get_area("D-1","viewer")
        self.assertEqual(area["status"],"resumed"); self.assertIsNone(area["current_reason"]); self.assertEqual(area["blockers"],[])
        with self.assertRaises(ConflictError): self.service.resume_area("D-1",{},"safety","safety_manager")
    def test_reincident_voids_resume_and_restops(self):
        item=self._item(area="E-1",severity="serious")
        self._close(item); self.service.resume_area("E-1",{},"safety","safety_manager")
        self._item(area="E-1",severity="minor",title="二次伤害")
        area=self.service.get_area("E-1","viewer")
        self.assertEqual(area["status"],"stopped"); self.assertIn("再发生事故",area["current_reason"])
        voided=[o for o in area["resume_orders"] if o["status"]=="voided"]
        self.assertEqual(len(voided),1); self.assertIn("再发生事故",voided[0]["void_reason"])
        summaries={a["area_code"]:a for a in self.service.list_areas("viewer")}
        self.assertEqual(summaries["E-1"]["status"],"stopped"); self.assertIn("再发生事故",summaries["E-1"]["current_reason"])
    def test_new_open_measure_voids_resume(self):
        item=self._item(area="F-1",severity="serious")
        self._close(item); self.service.resume_area("F-1",{},"safety","safety_manager")
        self.service.add_record(item["id"],{"kind":"corrective","detail":"复查发现新隐患","status":"open"},"recorder","investigator")
        area=self.service.get_area("F-1","viewer")
        self.assertEqual(area["status"],"stopped"); self.assertIn("新增未关闭措施",area["current_reason"])
        self.assertTrue(any("整改记录" in b for b in area["blockers"]))
    def test_audit_chain_and_item_area_code(self):
        item=self._item(area="G-1",severity="serious")
        self.assertEqual(self.service.get_item(item["id"],"viewer")["area_code"],"G-1")
        self._close(item); self.service.resume_area("G-1",{},"safety","safety_manager")
        self.assertTrue(self.repo.verify_audit_chain())
if __name__=="__main__": unittest.main()
