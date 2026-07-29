# 2026-07-24 第一/二批修复部署 runbook（需 root 窗口）

> 对应方案：docs/plans/2026-07-24-five-day-audit-fix-plan.md（v2 修订）
> 提交：worktree smartness-p0 `72524cf`(F2) + `15f45e0`(F1)；主树 `4eafc6f`(F3+feeder) + `6252484`(S4 基线)
> 铁律：每一步部署后必须过验证门 `scripts/verify_hk_deployment.sh`——本轮审计发现 projection_actor.py 和 binance_execution.py 两张补丁"放了没挂载"分别导致幽灵仓和孤儿止损。**禁止 docker cp / 进容器改文件**（验证门查不出）。

## 0. 前置

- [ ] H2 已完成（BTC 0.135 已有真实止损）——部署重启窗口内不允许存在裸仓。
- [ ] 低流量时段（建议 UTC 00:00-04:00 事件低谷），无未决 intent。
- [ ] 备份：所有被替换文件先 `cp -a X X.bak-20260724`。

## 1. 修 gen_recreate.py 挂载缺陷（结构性根因，先修这个）

现状：只显式追加 planner+contracts 挂载，其余靠继承旧容器 Mounts。改法：把 container-patches/ 下**全部**需生效补丁逐一显式 `-v` 挂载（projection_actor.py→/app/projection/actor.py、binance_execution.py→对应 nautilus 路径、本批新增文件按第 2 节映射），不再依赖继承。

## 2. 拷贝清单与目标映射

### 节点侧（两容器，经 container-patches + 重建）
| 仓库文件（smartness-p0） | hk container-patches | 容器内挂载点 |
|---|---|---|
| services/nautilus-node/runtime/exchange_cancel_adapter.py | exchange_cancel_adapter.py | /app/runtime/exchange_cancel_adapter.py |
| services/nautilus-node/strategy/intent_execution_planner.py | intent_execution_planner.py（**覆盖现有，先 diff 保留 hk 版 CANCEL_ORDER/GTD 增量**） | /app/strategy/intent_execution_planner.py |
| services/nautilus-node/app/node.py | node_app.py（按现有命名） | /app/app/node.py |
| services/nautilus-node/projection/event_mapper.py | event_mapper.py | /app/projection/event_mapper.py |
| （已存在）projection_actor.py | 已在，**本次必须挂上** | /app/projection/actor.py |
| （已存在）binance_execution.py | 已在，**本次必须挂上** | nautilus adapters 对应路径 |
| intent_execution_strategy.py | **不可直接覆盖**：线上 2230 行 hotpatch 分叉，需把 F1 hunks（收养/adapter 路由，见 15f45e0 中该文件 diff）手工合入线上版后放回 | /app/strategy/intent_execution_strategy.py |

### 控制面（宿主直跑，替换后重启服务）
| 仓库文件 | hk 目标 |
|---|---|
| services/control-plane/api/read_api.py | /srv/trader-v3/services/control-plane/api/read_api.py（先 diff：hk 版含 07-20 TP 方向校验等增量，需合并不是覆盖） |
| services/control-plane/db/repository.py | 对应 hotpatch 位置（07-07 补丁真身，先 diff 合并） |
| services/control-plane/order_management/{order_reducer,position_reducer}.py | 对应位置 |
| services/control-plane/tools/exchange_state_recorder.py | 对应位置 |
| packages/contracts/v1/order_state.v1.json | 对应位置 |

### 监控/feeder（主树 .live-mirror，直接拷贝+重启）
| 仓库文件 | hk 目标 | 重启 |
|---|---|---|
| .live-mirror/scripts/order_lifecycle_monitor.py | /srv/trader-v3/scripts/order_lifecycle_monitor.py | trader-v3-lifecycle-monitor.service |
| .live-mirror/scripts/hermes_signal_feeder.py | feeder 部署位置（基线文档有记录） | feeder 进程 |

## 3. 部署顺序

1. 监控/feeder（最低风险，无交易路径）→ 验证门 + 观察一轮 sweep 日志。
2. 控制面（read_api/repository/reducers/recorder）→ 重启 → `/ready` 全绿 + 发一条只读查询冒烟。
3. 节点容器重建（gen_recreate 修复版）→ **verify_hk_deployment.sh 强制双节点 mountinfo 核验** → `/ready` ACTIVE。
4. F2b 投影重建：`rebuild_positions_projection.py` 先 dry-run 审差异（应清 ETH×2/JTO 三个幽灵仓）→ 低流量 `--apply`（短暂 execution_events SHARE 锁 + positions ACCESS EXCLUSIVE 锁）。
5. 真单验收：对 JTO 孤儿止损（algoId 1000002464583088）下一条显式 cancel intent——成功撤到交易所侧即 F1 全链路闭环；随后同法清 ETH 孤儿单与 RUNE/HYPE 双份止损（替代 H1 手工撤）。

## 4. 验证门用法

```bash
# 宿主直跑文件：sha256 + hk 目标路径
sha256sum <本地文件> | awk '{print $1"  <hk目标路径>"}' > /tmp/manifest.txt

# container-patches 文件：必须追加容器目标路径
hash=$(sha256sum container-patches/projection_actor.py | awk '{print $1}')
echo "$hash /srv/trader-v3/container-patches/projection_actor.py /app/projection/actor.py" \
  >> /tmp/manifest.txt

bash scripts/verify_hk_deployment.sh /tmp/manifest.txt   # 非零退出=有差异，逐条列出
```

## 5. 回滚

各 .bak-20260724 原位放回 + 对应服务重启/容器重建（gen_recreate 用旧 Mounts 清单）。F2b 重建有 shadow 表，`--apply` 前的原表数据在事务外不受影响，回滚即不切换。

## 6. 已知遗留（部署后仍开放）

- /fapi/v1/algoOrder DELETE 响应字段未经真实环境验证（第 3.5 步真单验收即首次实测，失败则 adapter 只报错不误报成功，安全）。
- contract 未声明 cancel action，依赖线上既有 hotpatch；收编另立任务。
- mirror stale >300s 时新监控 sweep 有告警盲区；feeder attempts 进程内状态重启清零。
- DB 密码轮换（H3）与本部署无耦合，仍待做。
