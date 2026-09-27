from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='工伤事故调查与纠正措施'; ENTITY='事故'; ID_PREFIX='OI'
SEVERITIES=['minor', 'moderate', 'serious', 'fatal']; STATES=['reported', 'investigating', 'corrective_action', 'verification', 'closed']; TRANSITIONS={'reported': ['investigating'], 'investigating': ['corrective_action'], 'corrective_action': ['verification'], 'verification': ['closed'], 'closed': []}; TRANSITION_ROLES={'investigating': ['investigator'], 'corrective_action': ['investigator'], 'verification': ['safety_manager'], 'closed': ['safety_manager']}
CREATE_ROLES=set(['reporter', 'investigator']); RECORD_ROLES=set(['investigator', 'safety_manager']); AUDIT_ROLES=set(['safety_manager', 'viewer']); VIEW_ROLES=set(['reporter', 'investigator', 'safety_manager', 'supervisor', 'viewer'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'serious': 6.0, 'fatal': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'serious': 8, 'fatal': 4}; TERMINAL_STATES=set(['closed'])
AREA_ENTITY='区域停工单'; AREA_RESUME_ENTITY='区域复工单'
AREA_SCOPES=['equipment', 'whole_area']; AREA_ORDER_STATES=['stopped', 'resumed', 'superseded']; AREA_RESUME_STATES=['rejected', 'active', 'void']
AREA_WIDE_SEVERITIES=set(['serious', 'fatal'])
AREA_STOP_ROLES=set(['supervisor', 'safety_manager']); AREA_RESUME_ROLES=set(['safety_manager']); AREA_VIEW_ROLES=set(['reporter', 'investigator', 'safety_manager', 'supervisor', 'viewer'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def default_stop_scope(severity):
    return 'whole_area' if severity in AREA_WIDE_SEVERITIES else 'equipment'
def normalize_area_scope(value,severity):
    expected=default_stop_scope(severity)
    if value is None: return expected
    if value not in AREA_SCOPES: raise ValidationError("scope不在允许范围内")
    if expected=='whole_area' and value!='whole_area':
        raise ValidationError(f"{severity}事故必须停整片区域，不能只停单台设备")
    return value
def resume_blockers(linked_item_open, linked_open_records, area_open_item_ids,
                    area_open_record_item_ids):
    blockers=[]
    if linked_item_open:
        blockers.append("停工单关联事故尚未关闭")
    if linked_open_records:
        blockers.append("关联事故的整改记录尚未全部关闭")
    if area_open_item_ids:
        blockers.append("区域内仍有未结事故："+
            "、".join("#"+str(i) for i in sorted(area_open_item_ids)))
    if area_open_record_item_ids:
        blockers.append("区域内仍有未关闭整改措施（事故："+
            "、".join("#"+str(i) for i in sorted(area_open_record_item_ids))+"）")
    return blockers
