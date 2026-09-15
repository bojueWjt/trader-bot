# 手机账户权益曲线回归修复

## 现场证据

- 手机 `com.pudutech.attention` 显示 `equity history payload missing equity_history_total`。
- 2026-09-15 23:12 UTC，线上 `GET /v1/accounts?history_hours=24` 返回 200，但只有当前账户列表，没有契约 §8 要求的 `data.equity_history_total` 和 `data.equity_history_meta`。
- 线上 `read_api.py` 没有历史读取函数；`exchange_state_recorder.py` 没有历史采样函数。两个线上文件与修复前本地 HEAD `a9be037` 完全一致。
- 既有 `account_equity_samples` 仍在，四账户各 509 条，最早 2026-09-05 08:00 UTC，最新桶 2026-09-15 22:00 UTC，最后采样约 22:15:44 UTC。
- 9 月 14 日发布备份含采样实现，9 月 15 日 22:12/22:14 发布备份不含实现。原功能提交为 `d027205`。证据表明新版遗漏已有功能，导致读取和后续采样同时回归；手机配置并非本次报错原因。

## 修复范围

- 恢复原 `/v1/accounts?history_hours=1..168` 历史字段；不带参数的旧响应保持原样。
- 沿用 `account_equity_samples` 和既有 recorder，在成功快照事务内按半小时桶更新；历史写入失败通过 savepoint 隔离，保留账户快照。
- 总额仅统计现有账户注册表内的账户，预期账户数来自同一注册表。缺账户不能因为投影行也缺失而被误认为完整；排除未来样本，保留十进制精度。
- 不新增表、服务、接口或第二采样器；不伪造停采期间的历史点。

## 验证

真实临时 PostgreSQL + 现有 recorder 回归：37 passed，1 subtest。包括无参数兼容、空历史、半小时去重、缺账户、未注册账户排除、精度、7 天窗口上限、未来样本排除、鉴权与采样失败隔离。

命令：

```sh
/Users/balen/.grok/venvs/trader-bot-pytest/bin/python -m pytest \
  tests/control-plane/api/test_accounts_equity_history.py \
  tests/control-plane/api/test_exchange_state_recorder_equity_samples.py \
  tests/control-plane/tools/test_exchange_state_recorder.py -q
```

## 线上生效步骤（尚未执行）

1. 核对线上两个文件仍为审查基线，既有表与 operator-query SELECT 权限可用；语法检查全部通过后备份。
2. 替换这两个文件，保持权限，重启 `trader-v3-controlplane-operator-query.service` 和 `trader-v3-exchange-state.service`。
3. 验证 24h/168h 返回历史字段，四账户恢复新采样，手机刷新后曲线出现。
4. 任一步失败恢复两个原文件并重启对应服务。交易节点、交易闸门和手机连接配置无需调整。

安装新 APK 不能代替后端修复；现有手机包已请求正确契约，服务修复后可直接刷新验收。
