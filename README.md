# 海底观测网设备故障管理

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8337`。领域对象包括站点、资产、链路、遥测、故障事件、恢复动作、出海任务和数据缺口。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8337
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8337/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。

## 迟到遥测重算流程

科考船迟到回传遥测后，故障严重度、恢复方案和出海任务不再沿用旧依据，整条链路在一次事务内重算：

1. **遥测修订**：`telemetry`的`revise`动作只接受**严格更高**的修订号，低修订号直接冲突且状态保持不变。
2. **记录依据**：创建故障事件时可用`basis_telemetry_id`（或`asset_id`+`metric`）关联遥测；未显式给`severity`时按该修订值自动重算严重度。生成恢复方案/出海任务时，会把`basis_telemetry_id`、`basis_revision`抄录到方案上。
3. **级联失效**：修订一更新，同事务内
   - 关联事件的`severity`与`basis_revision`立即重算（写`recalculate`审计）；
   - **未执行**的恢复动作（`proposed`/`approved`）退回`proposed`、出海任务（`planned`/`approved`）退回`planned`，并打上`stale=true`、`invalidated_by_revision`（写`invalidate`审计）；
   - **已执行/已完成/进行中**的动作与任务留档不动。
4. **重新确认**：失效对象在工程人员（`engineer`/`admin`）执行`reconfirm`前不能`approve`/`start`/`depart`；`reconfirm`刷新`basis_revision`、清除`stale`并记录`reconfirmed_by`，之后状态才能继续推进。值班员（`operator`）与巡检员（`field`）均不能重新确认。

## 并发、越权与重试

- 同一资产同一故障类型只能有一个活动事件，同一`dedupe_key`只能有一个未终结恢复动作；规则层有预检查，数据库还有部分唯一索引兜底，因此**两个值班员并发提交同一故障只有一方落库，后到者得到409冲突（含当前修订号）**。
- 遥测修订带`expected_version`乐观锁 + 单写事务，并发修订同样只有一方成功。
- 巡检员（`field`）只有只读权限，越权提交返回403且**原状态完全保留**。
- 失败后重试：带相同`Idempotency-Key`（请求头）的重复提交直接返回已有结果，不会二次落库；未完成对象可安全重放，已完成对象返回原结果。已终结（`failed`/`succeeded`/`cancelled`）的动作可凭同一`dedupe_key`另开新方案，旧动作留档。

## 核心流程

建立站点、资产和链路后记录遥测与故障事件，创建恢复动作并跟踪重启、备用链路、出海任务和数据缺口，最后关闭事件。遥测`revise`动作只接受更高修订号，用于处理迟到数据。

## 规则重点

- 遥测迟到修订只接受更高修订号，修订后级联重算严重度、恢复方案和出海任务（见上）。
- 同一资产和故障类型不能同时有多个活动事件（规则 + 数据库部分唯一索引双重保证）。
- 恢复动作按`dedupe_key`防止重复执行；失效方案必须经工程人员`reconfirm`后才能继续。
- 恢复方案、出海任务保留`basis_revision`，已执行对象不因新修订被改写。
- 事件解决前恢复动作、数据缺口和受影响资产必须达到可关闭状态。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
