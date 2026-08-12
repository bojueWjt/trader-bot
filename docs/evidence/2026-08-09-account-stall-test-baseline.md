# Account Stall Test Baseline

日期：2026-08-09
用途：冻结重新复盘时的本地测试环境、选择范围、结果和 collection inventory
证据边界：macOS checkout；不覆盖 Linux、Docker、Redis、PostgreSQL 或生产行为

## 1. Checkout 与依赖

| 项目 | 值 |
|---|---|
| HEAD | `7368641e98c410b3c6edbe4ab3ea8f72cf5efe1d` |
| Branch | `codex/account-stall-hardening` |
| Worktree | 61 tracked changes，107 untracked，168 status entries |
| Host | Darwin 25.5.0 arm64 |
| uv | 0.11.26 |
| Python | 3.12.13 |
| pytest | 9.1.1 |
| Nautilus Trader | 1.227.0 |
| pydantic | 2.13.4 |
| jsonschema | 4.26.0 |
| fastapi | 0.141.1 |
| httpx | 0.28.1 |
| psycopg2-binary | 2.9.12 |

依赖通过 `uv run --no-project --with ...` 临时解析。当前仓库没有项目级 pytest 配置，
运行时出现未知 `hk_nautilus` marker warning；该 warning 不改变选择范围。

## 2. Runtime / Nautilus

命令：

```bash
uv run --no-project --with pytest --with 'nautilus_trader==1.227.0' \
  --with 'pydantic>=2.7,<3' --with jsonschema --with fastapi \
  --with httpx --with psycopg2-binary \
  python -m pytest -q tests/execution tests/nautilus
```

结果：

```text
4 failed, 523 passed, 11 skipped, 19 warnings, 22 subtests passed
```

四个失败均位于 `tests/nautilus/runtime/test_node_config_risk.py`：

```text
test_live_node_requires_release_bound_runtime_resources
test_live_node_requires_release_bound_redis_memory_thresholds[memory_warning_ratio]
test_live_node_requires_release_bound_redis_memory_thresholds[memory_degraded_ratio]
test_live_node_requires_release_bound_redis_memory_thresholds[memory_critical_ratio]
```

这些测试期望 live 配置缺完整 runtime resources 或 Redis memory threshold 时抛出
`NodeConfigError`。当前 `_requires_explicit_runtime_resources` 受两个环境变量共同控制，
测试环境未设置该组合，四项均以 `DID NOT RAISE` 失败。

## 3. Release 聚焦套件

命令：

```bash
uv run --no-project --with pytest --with 'pydantic>=2.7,<3' \
  --with jsonschema --with fastapi --with httpx --with psycopg2-binary \
  python -m pytest -q \
  tests/deployment/test_make_account_stall_release.py \
  tests/deployment/test_build_immutable_node_image.py \
  tests/deployment/test_release_manifest.py \
  tests/deployment/test_account_stall_systemd_resources.py \
  tests/nautilus/runtime/test_node_config_risk.py
```

结果：

```text
48 failed, 29 passed
```

失败集中在 systemd schema/consumer 漂移、dependency lock/source-manifest fixture 路径、
strict v3 runtime resources fixture 和四个 live runtime fail-open 测试。

## 4. Deployment 聚焦套件

命令：

```bash
uv run --no-project --with pytest --with 'pydantic>=2.7,<3' \
  --with jsonschema --with fastapi --with httpx --with psycopg2-binary \
  python -m pytest -q \
  tests/deployment/test_account_stall_operation_lock.py \
  tests/deployment/test_hk_control_plane_isolation.py \
  tests/deployment/test_hk_deploy_control_plane_roles.py \
  tests/deployment/test_hk_deploy_evidence_gate.py \
  tests/deployment/test_hk_deploy_redis_v2_gate.py \
  tests/deployment/test_hk_redis_rebaseline.py \
  tests/deployment/test_redis_namespace_janitor_lock.py \
  tests/deployment/test_reviewed_release_rollout.py
```

结果：

```text
34 failed, 36 passed, 2 skipped, 30 subtests passed
```

失败集中在 Redis resource 默认路径、Shell guard fixture、新增全局输入和 rollout strict v3
fixture。GAP-7 的真实级联范围需要 Linux `flock`、真实 migration runner 和 fence env 的
red run 继续确认。

## 5. Maintenance Fence / Rollout

命令：

```bash
uv run --no-project --with pytest --with 'pydantic>=2.7,<3' \
  --with jsonschema --with fastapi --with httpx --with psycopg2-binary \
  python -m pytest -q \
  tests/control-plane/db/test_maintenance_fence.py \
  tests/control-plane/api/test_reviewed_release_rollout.py
```

结果：

```text
8 failed, 8 passed
```

maintenance fence 数据层 8 项通过。8 个失败均来自 rollout 调用未传新增的
`operation_lock` 参数。

## 6. Collection Inventory

每组使用相同依赖与 path list，将执行命令中的 `python -m pytest -q` 替换为：

```bash
python -m pytest -p no:cacheprovider --collect-only -q
```

随后生成稳定的排序 node-id inventory：

```bash
LC_ALL=C rg '^tests/.*::' /tmp/<suite>.collect.txt \
  | sort > /tmp/<suite>.nodeids.txt
shasum -a 256 /tmp/<suite>.nodeids.txt
```

| Suite | Collected | Sorted node-id SHA-256 |
|---|---:|---|
| runtime/Nautilus | 538 | `ba63a2910ff00cd7f0db6e548b40f21c112e245993bd300217ac3e752ce29b7a` |
| release focused | 77 | `5a325a1096a6379e73aa445ce5a0b27864fac275dd03652bbe0ad34b4eb5cf9f` |
| deployment focused | 72 | `2a33079315e1520c59d83651b375d30df08edfc6dec6915dc10f43acfaa4117e` |
| fence/rollout | 16 | `5ce8c43f005275304392788bd001953643c90fa5687b5ea18c3d6bd82a76e5cd` |

Phase A 必须将 node-id inventory 作为版本化证据保存到干净 worktree。后续结果只有在
inventory hash 相同或差异已逐项解释时，才能与本基线比较。

## 7. 491 与 523 的边界

`docs/incidents/2026-08-08-account-node-stall.md` 记录的
`491 passed, 21 skipped, 22 subtests passed` 产生于 2026-08-08。该次运行缺少冻结的
collect-only inventory、依赖快照和当前四个 resource contract 测试的选择证据。

2026-08-09 基线收集 538 项，结果为 523 pass、4 fail、11 skip。两次结果对应不同日期、
不同工作树和不同测试 inventory。它们分别证明各自时点的测试面，不能用于计算新增失败
或修复数量。

## 8. Nautilus 真实入口

MessageBus host integration：

```text
tests/nautilus/projection/test_projection_hk.py::
ProjectionHostNautilusTests::
test_projection_actor_receives_real_nautilus_on_event_callbacks
```

`tests/nautilus_simulated_harness.py` 是共享 harness 库。七个实际测试由以下文件导入：

- `tests/execution/open/test_intent_execution_strategy_hk.py`：2 项
- `tests/execution/manage/test_intent_execution_strategy_manage_hk.py`：3 项
- `tests/nautilus/risk/test_nautilus_integration_hk.py`：2 项

Phase B 的 gate 使用上述真实测试入口。
