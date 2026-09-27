# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8311
```

默认端口为`8311`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`（可带`area_code`挂接区域）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/area-orders[?area_code=]`：区域停复工台账列表
- `POST /api/area-orders`：现场负责人登记停工
- `GET /api/area-orders/{id}`：停工单详情（含历次复工单）
- `POST /api/area-orders/{id}/resume`：安全员提交复工，必须提交`expected_version`
- `GET /api/audit`

允许角色：reporter, investigator, safety_manager, supervisor, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 区域停复工台账

事故发生后车间可能只停出事设备，因此停工按区域登记，而不是随事故直接停线：

- **登记停工**（supervisor / safety_manager）：提交`area_code`（区域编号）、`reason`（停工原因）、`affected_shifts`（影响班次数组）；可选`item_id`关联事故、`scope`（`equipment`只停设备/工位或`whole_area`整片区域）、`equipment_tag`（设备/工位标识）。关联事故为`serious`/`fatal`时`scope`只能是`whole_area`，默认也为整片区域；`equipment`必须填写`equipment_tag`。区域仍在停工时重复登记返回409并带当前原因。
- **复工阻断**（仅safety_manager）：关联事故未关闭、关联事故整改记录未关闭、区域内有未结事故、区域内有未关闭整改措施，任一存在即退回（409），响应和退回复工单都写清`blockers`阻断项；阻断解除后复工成功。
- **复工后失效**：同区域再登记事故，或给同区域事故新增`open`整改记录时，原复工单自动置为`void`（带作废原因）、停工单回到`stopped`，`current_reason`更新为本次原因；详情和列表都返回`current_reason`，停工中还会实时返回`blockers`。
- 复工单（`area_resumptions`）保留`rejected`（退回）/`active`（有效）/`void`（作废）全链路，均可在停工单详情中查看，并写入审计链。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
