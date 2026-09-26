# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。

## 冻结签署会话

重要培养方案的学期冻结需经教务（`academic_affairs`）、学院（`college`）、审计（`audit`）三方代表签署后才能正式发布，相关接口位于 `/api/plans/{plan_version}/sign-sessions/{session_id}` 下：

- `POST .../sign-sessions/{session_id}` 发起会话：此时固定快照内容（含事件截止点），并指定三方代表、法定人数（默认 3）与可选的有效期（`ttl_seconds` 或 `expires_at`）；同一会话标识重复发起幂等返回原会话。
- `POST .../signatures/{role}` 签署：仅该角色当前代表可签署；重复签署幂等，撤回后重签重新生效。
- `POST .../signatures/{role}/withdraw` 撤回：只在发布前（且会话未过期）有效。
- `POST .../delegates/{role}` 替换代表：人员变更或利益冲突回避时更换代表；原代表签名保留但不再计入有效票，快照内容不变；同一人不得同时担任多个角色代表。
- `POST .../publish` 发布：有效票达到法定人数才把会话固定的快照落库为冻结；重复发布幂等。

会话过期或已发布后，签署、撤回、替换代表与发布均被拒绝；发布后的冻结内容可通过既有的 `/api/plans/{plan_version}/freezes/{freeze_id}` 接口读取。
