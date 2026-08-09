# Account Stall Hardening 团队花名册

| 角色 | Codex 模型 | 市场来源 | 裁剪后的职责 |
|---|---|---|---|
| 总控与变更负责人 | `gpt-5.6-sol` high | Agents Orchestrator | 维护任务图、质量门、生产变更门和最终集成 |
| 交易运行时架构师 | `gpt-5.6-sol` high | Software Architect | 输出 ADR、模块边界、状态机与迁移顺序 |
| 节点并发与生命周期工程师 | `gpt-5.6-luna` medium | Backend Architect | 实现 lanes、队列、watchdog、幂等和生命周期 |
| 控制面与 PostgreSQL 工程师 | `gpt-5.6-luna` medium | API Platform Engineer | 拆分关键 API、连接池、超时、协议兼容 |
| Redis 与不可变发布工程师 | `gpt-5.6-luna` medium | SRE | 稳定 namespace、容量治理、镜像和发布门 |
| 高风险代码审查员 | `gpt-5.6-terra` high | Code Reviewer | 只读审查正确性、安全、并发、幂等和回滚 |
| 故障注入与恢复验收工程师 | `gpt-5.6-terra` medium | Performance Benchmarker | 执行真实依赖、压力和恢复场景 |
| 真实小额交易安全验证官 | `gpt-5.6-sol` high | Autonomous Optimization Architect | 独占真实交易权限，执行受限 canary 与熔断 |
| 证据与发布审计员 | `gpt-5.6-terra` medium | Evidence Collector | 核验四层证据和 release metadata |
| 运行手册与 ADR 文档员 | `gpt-5.6-luna` low | Technical Writer | 更新 ADR、runbook、迁移和验证文档 |

## 编制说明

- 三个 Executor 按故障域分工，可在 ADR 冻结后并行工作。
- Reviewer 和 Chaos Tester 构成代码与运行双质量门。
- Evidence Auditor 对完成声明进行独立复核。
- Live Trade Verifier 是唯一拥有真实下单权限的角色。
- Planner 是唯一集成者和生产变更批准者。

选择结果存档于 `.agent-team/team-selection.json`。
