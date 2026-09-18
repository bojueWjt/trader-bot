# 2026-09-18 平仓反馈与比例减仓修复

状态：后端提交 `b9ba8079bc0f0112ffe8143fb8ed902d313c6def` 已部署 jp-24；用户明确授权 RESUME 后，A–D 已恢复 ACTIVE，并通过新审计和两轮递增心跳核验。Android 构建完成，手机待用户连接后安装。未重放任何交易。

## 结果与边界

- Hermes `partial --percent X` 发送结构化 `fraction=X/100`，不读取旧 projection 换算数量，也不解析 reason 文本。
- API partial_close 接受 quantity 或 fraction 二选一；fraction 必须满足 `0 < f <= 1`，写入 order_plan 并参与既有幂等摘要。已有 quantity 请求保持兼容。
- 节点 channel 请求使用 `min(robot_owned, fresh same-side venue) * fraction`，按 step 向下量化；user 请求保持同向 venue 授权范围。计划和提交前授权上限使用相同基数。
- 缺少新鲜 venue 或机器人持仓归属时拒绝；不会把 channel 提升为 user，不修改 stash。robot_owned 沿用现有同账户、同品种、同方向机器人归属，未新增 per-channel 分账。
- App 分别显示“正在提交”“已提交，正在确认成交”“已平仓/已减仓”。仓位消失后保留本次结果，禁用旧快照上的新写操作；结果按账户、币种和方向隔离，后续改止损不会继承减仓提示。
- D/BTC 先前实际成交已查实；本次未重复平仓。TAO 原请求未成交，修复不等于补执行。历史 stash 缺口见调查记录，无法由当前文件重建当时授权余额。

## 验证

后端工作树：`/Users/balen/projects/trader-bot/.worktrees/tao-reduction-authority-20260918`，分支 `codex/tao-reduction-authority-20260918`，基础提交 `bc0d22a`。

```sh
PYTHONPATH=/Users/balen/projects/trader-bot/.venv-arch/lib/python3.12/site-packages \
  .venv-nautilus-1.227/bin/python -m pytest -q \
  tests/execution/manage/test_hedge_reduce_only_submission.py \
  tests/execution/manage/test_intent_execution_planner_manage.py \
  tests/test_v3_trade_routing.py
# 122 passed, 41 subtests passed; exit 0

/Users/balen/projects/trader-bot/.venv-arch/bin/python -m pytest -q \
  tests/control-plane/api/test_user_management_priority.py \
  tests/control-plane/api/test_operator_account_registry.py
# 58 passed; exit 0
git diff --check
# exit 0
```

实际运行 Nautilus 1.227.0，Python 3.12，macOS arm64；未以 stub/skip 代替节点提交测试。wheel SHA-256 `735fbbc0737be8f945ee641aeb0dbf0ea6b4c6111f11f10c244fe198f8158953` 与 `infra/docker/nautilus/uv.node.lock` 一致。使用本机 pip 缓存离线安装，未改全局包。存在 NumPy/Starlette 依赖弃用警告。目标 Linux 发布镜像内另运行两个节点测试文件：50 tests、exit 0；容器隔离网络、只读文件系统，没有生产凭据或生产状态挂载。

覆盖关键值：venue 9.890 / owned 0.308 / fraction 0.3 → 0.092；owned 9.890 → 2.967；无 owned 拒绝；多空与品种隔离；owned 超过 venue 时截至 venue；批准上限、数量与比例互斥、非法比例和幂等冲突。

App 工作树：`/Users/balen/projects/working/alert-personal/.worktrees/close-visible-result-20260918`，分支 `codex/close-visible-result-20260918`，基础提交 `6ef42579fbad1b7bf5e4e992c6b2cc4739e48098`。

- 5 个相关 Jest suite：110 passed，exit 0（包含 screen、operatorIntentState、tradingApi、颜色检查）。
- `npm run typecheck`：exit 0。
- `./gradlew :app:assembleRelease -PattentionAllowDebugReleaseSigning=true -PreactNativeArchitectures=arm64-v8a`：BUILD SUCCESSFUL，exit 0。
- 最终 APK：`/Users/balen/projects/working/alert-personal/dist/close-visible-result-20260918/attention-android-0.1.0-close-visible-result-r3-arm64.apk`。
- APK SHA-256：`00bf165e7023ded2c0f10dbbd9bd70c70fb7ea31beccd5f774cc73729a1d97d1`。
- 包名 `com.pudutech.attention`；签名 SHA-256 `fac61745dc0903786fb9ede62a962b399f7348f0bb6f899b8332667591033b9c`，与先前安装源一致。
- adb 未发现手机 `1db8b582`；用户表示稍后连接，未安装、未做真机交易验证。

## 上线预检与回滚计划（执行前制定）

1. 在 jp-24 保持原节点运行，核对当时实际代码/镜像、服务、心跳与审计；不能把本工作树基础版本或别的会话汇报当线上版本。先审查待部署基础提交与当前线上版本差异，保留已有改动。
2. 仅在 `/srv/trader-staging` 准备版本化发布目录、目标 Linux 镜像与回滚镜像；不得使用 jp-24 的 `/tmp`。本次涉及 operator-query API、Nautilus planner/strategy、Hermes CLI/SKILL，无数据库 schema 迁移。
3. 停任何节点前完成全部部署门禁：目标镜像与 manifest 哈希、依赖和目标环境回归、单写者围栏、账实对账、磁盘/Redis容量，以及 fault report 1h / capacity evidence 24h 新鲜度。任一必需门禁失败即中止，保持旧版本运行。不得依赖旧文档的降级条款放宽用户门禁。
4. 顺序应为兼容扩展 API → 支持 scope fraction 的节点 → Hermes CLI/SKILL；不能先启用只发送 fraction 的新 CLI 而让旧节点按整簿计划。控制面代码更新后重启对应 operator-query 服务并验版本，不能仅落盘。节点逐账户更新必须在获准维护窗口执行。
5. 部署授权不等于 RESUME。若流程会使账户 HALTED，须事先取得用户明确的恢复交易指令及相应门禁条件，不能部署后自行开闸，也不能先停机再等待授权。
6. 生效验收查看持续增长的心跳、实际 release_id、ready、账户状态、节点拒因及审计。只读核对，不用真实订单作为探针，不撤用户手动单，不补发 TAO/BTC 旧请求。
7. 回滚先停止新 CLI 的 fraction 请求入口，核对已受理及在途意图，再按已验证 manifest 恢复旧节点与 API；不能把仍在途的 fraction 意图交给会按全簿计算的旧节点。不得转换旧请求数量、重置去重键或重放交易。保留 stash、inbox、订单、数据库与审计记录。回滚同样不授权 RESUME。

Grok 完成主要实现与 App 构建。最后 companion 会话在 session_create 阶段超过 4 分钟无任务输出并报网络同步错误，且无 Grok MCP 工具，按 GROK.md 第 10 条由 Codex 接手：从已有缓存完成节点环境、运行真实节点测试、补方向/venue 上限接线回归，并去掉缺失 venue 时的虚构数量上限。

## 2026-09-18 部署与 RESUME 验收

- 用户的 `RESUME` 授权覆盖预检后部署和恢复维护前 ACTIVE 的账户；维护前基线确认 A–D 均 ACTIVE。
- 首次归档因 macOS 附加文件触发 dashboard exact-set 门禁，在停节点前退出；用 Python tarfile 重打纯净归档后预检成功。未跳过完整性校验。
- 执行采用现有 `maintenance_fence` 流程、`SKIP_RESUME=1`；该路径统一重建 A–D，并非逐账户滚动。API/CLI 安装期间四节点已静止，随后对应控制面与 Hermes 服务重启。
- 实际发布 release_id：`b50ce92e48b2fb8b13f19485c174456640c291c4a040ec839ab8db7d500a5c4c`。预检 manifest 因备份上下文不同有另一个 release_id，以执行发布和实际心跳为准。
- 四节点镜像：`sha256:84f0ac9d6e70d2e0300b6684d280350986b5db8dedbb74ee081b87b210d75152`。43 个发布目标哈希、各节点配置与标签验证通过；另独立核对四节点 planner/strategy 以及宿主 API/CLI/SKILL 哈希。四节点 RestartCount=0。
- 回滚备份：`/srv/trader-v3/backups/deploy-20260918T144953Z-account-stall-hardening`。数据库 schema 未改变。
- 预检保留既有 `reviewer-trust proof is missing` advisory，未声称具备独立签名 reviewer 证明；该流程返回 PREFLIGHT OK 与 DEPLOY OK。

RESUME 新命令（均由 API 受理，operator_commands 和对应节点 ACK 均 completed，审计 release_id 与当前发布一致）：

| 账户 | command_id | 最后两轮 heartbeat_sequence |
| --- | --- | --- |
| A | `65ea5473-dde3-49c7-8cd3-5faa383b3b05` | 321 → 326 |
| B | `adc65ce5-a0ba-4822-952b-0a24dfd81b9b` | 323 → 328 |
| C | `085f8c1f-7b51-4e7a-bbf1-87254e5316ba` | 314 → 319 |
| D | `0d20a8bb-72cd-441d-ab29-4bb06bbb4ebc` | 310 → 315 |

两轮均 ACTIVE、ready=true、reconciliation healthy/fresh、heartbeat age <5s、restart_required=false；无未关闭 P0/P1。未用真实交易作为验收探针。

### 维护中发现的 B/GLM 状态覆盖

GLM 意图 `5b42d41b-a99b-41d8-9e44-084547e33519` 在 14:45 UTC 已获批、受理，首腿已成交，第二腿 LIMIT 与保护单仍在交易所。14:51:50 UTC 节点重启后的 `halted` ACK 将其从 approved 改为 rejected，导致 RESUME 的既有订单门禁拒绝 B。

从维护前数据库备份核实原 status=approved，结合原 accepted 审计、27 条首腿 fill 事件和新鲜交易所第二腿证据，仅恢复该行 status。原分发 valid_until=15:00:44 UTC；修复在 15:01:19 UTC、窗口到期后执行，因此不会再次被意图轮询取出。事务内重新运行未修改的订单门禁并通过，再提交恢复审计 `0382f9d5-0771-4c93-832f-0b9d476c86f1`。未改数量、价格、批准时间、幂等键、订单或保护单；验收确认维护后没有该 GLM 意图的新执行事件。

剩余问题：本次完成的是这条状态的证据化恢复；通用的“重启后迟到 halted ACK 覆盖已受理意图”代码路径尚未修复，后续维护仍需处理该状态机缺陷。不能将本次单行恢复描述为根治。

远端证据目录：`/srv/trader-staging/tao-scope-b9ba807-20260918T143700Z`。
本地证据目录：`/Users/balen/.codex/artifacts/tao-scope-b9ba807-20260918T143700Z/evidence`，核心文件为 `final-acceptance.json`、`runtime-verification.json`、`commands.jsonl`、`glm-status-recovery-evidence.json`、`preflight.log` 和 `deploy.log`。

部署前 companion 恢复会话再次停在 session_create 超过 3 分钟，未开始任务执行；已取消。因无 Grok MCP 且 companion 无法启动，按 GROK.md 第 10 条由 Codex 完成部署和验收。
