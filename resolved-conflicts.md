# C-00 冲突解决记录

- **git 层冲突：0。** A fast-forward；B 三方合并无冲突（B 用 `tests/nautilus/_fixtures/contracts-v1/` fixtures，未改 A 的 `packages/contracts`）。
- **语义层分歧（非 git 冲突，C 裁决）：** 见 `MERGE_REPORT.md` §3–§5 —— `order_plan` 闭合空对象(P0)、`idempotency_key` pattern、seam 端点缺失、A 的 11 个 P0。
- 无需手工 git 冲突解决；contracts-lock 与 C-01 阶段处理语义对齐。
