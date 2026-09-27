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
- `POST /api/items`（可带`area_code`关联区域）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`
- `GET /api/areas`，区域停复工台账列表，标出每个区域当前状态与当前原因
- `GET /api/areas/{area_code}`，区域详情：停工单、复工单、当前阻断项
- `POST /api/areas/{area_code}/stop`，现场负责人登记停工：`reason`、`shifts`
- `POST /api/areas/{area_code}/resume`，安全员提交复工，被退回时返回`blockers`

允许角色：reporter, investigator, safety_manager, site_supervisor, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 区域停复工规则

- 现场负责人（site_supervisor）登记区域编号、停工原因和影响班次；区域已停工时重复登记返回冲突。
- 严重事故（serious/fatal）登记后默认停整片区域，自动生成停工单，影响班次记为全部班次。
- 区域内整改记录未关闭或仍有未结事故时，安全员（safety_manager）提交复工会被退回（409），响应与台账中的复工单均写清阻断项。
- 复工后同区域再发生事故或新增未关闭措施，原复工单作废，区域自动回到停工，列表与详情的`current_reason`标出当前原因。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
