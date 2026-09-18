# App 安装与收益历史接口上线

- App 源码：`alert-personal` 提交 `20b852e8b2e52bbe511cad3da3853bd39e8ebc3a`。在源 Mac 独立 worktree 构建 arm64 Release 联调包，351 项 Gradle task 完成，未修改原 App 工作目录。
- 手机：`2509FPN0BC`，ADB `1db8b582`。`adb install -r` 返回 Success，原安装数据保留；启动 Status ok，进程存活。APK SHA-256：`87b82717ab61519067f796bbbf234b823515dd0f40e2386f637eff74cb52a110`。
- 安装后用户报告 30D / 365D 返回 400；生产接口仍限制 `history_hours <= 168`。App 先更新而配套查询接口未上线导致版本不匹配。
- 从 `5b0858f` 提取已测试的收益历史代码，仅替换生产 `_parse_history_hours` 至 `_equity_history` 区段，保留其他生产差异。上限 8760 小时，30D 取 6 小时快照，365D 取日快照。
- 上线前：13 项 PostgreSQL 回归通过；候选文件语法通过；生产数据库只读预检 24/168/720/8760 小时均正常，查询耗时 5–8ms。原生产 HTTP 结果为 200/200/400/400。
- 2026-09-17 16:30:51 UTC 完成查询服务更新：仅重启 `trader-v3-controlplane-operator-query.service`，PID `141536 -> 2628898`。24H/7D/30D/365D 均 HTTP 200，分别返回 48/334/50/13 个真实采样点。手机 30D 曲线已目视验证恢复；365D 已接口验证。
- 生产文件：`/srv/trader-v3/services/control-plane/api/read_api.py`；SHA-256 从 `f4844daeb746a924a66694dee91e588715e9864880067a7031bc17e3a87ab2a6` 变为 `36b9dbe0c66f7b205c4922af4cbfd2253131159246642d813b074d29785273e1`。
- 备份、预检、部署结果、失败自动回滚脚本：jp-24 `/srv/trader-staging/equity-history-20260917/`。本机证据在 `.release-out/equity-history-20260917/`；APK 与安装记录在 `/Users/balen/projects/alert-personal/dist/install-20260917/`。

本次没有重启交易节点，没有执行交易、撤单或 RESUME。之前 `bf87600` 分支中的用户授权、管理子单及平仓后保护单清理修复仍未上线，不能将本次查询服务更新视为整批修复已部署。
