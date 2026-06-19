# trader-bot

交易机器人大仓（monorepo），整合两部分：

```
trader-bot/
├── engine/   # freqtrade 交易引擎（fork，来自原 trader 仓库 develop 分支）
└── bridge/   # 桥接应用层：信号导入、Telegram 监听、Dashboard/API、部署脚本
```

## engine/

完整的 [freqtrade](https://github.com/freqtrade/freqtrade) fork，作为交易执行引擎。
开发与运行方式同上游 freqtrade（见 `engine/README.md`、`engine/docs/`）。

## bridge/

在 freqtrade 之上构建的自有应用层，把外部交易信号桥接到 freqtrade 实例：

- `apps/api/` —— Dashboard / API 后端（FastAPI），含鉴权与风控接口
- `apps/dashboard/` —— 前端 Dashboard
- `services/telegram-watcher/` —— Telegram 信号监听服务
- `freqtrade/signal_strategy/` —— 自定义信号策略（freqtrade 走 pip 依赖安装）
- `scripts/` —— 部署（`deploy_hk.sh`）、对账（`reconcile.py`）、回放冒烟脚本
- `docker/`、`docker-compose.yml` —— 容器编排
- `docs/`、`PLAN.md` —— 设计与里程碑文档

bridge 把 freqtrade 当作依赖/外部实例调用，不内嵌完整 freqtrade 源码。
需要修改引擎本体时改 `engine/`，需要修改信号桥接与面板时改 `bridge/`。

## 来源

本仓库由原先两个 git worktree 合并而来（`trader/develop` + `trader-bridge/bridge-import`），
以当前内容做了一次干净初始提交，未保留上游 freqtrade 的历史。
如需同步上游 freqtrade 更新，单独 fork 上游引擎再合入 `engine/`。

## 安全

仓库内不含真实密钥：交易所 `key`/`secret` 走环境变量注入，
配置文件中的 `jwt_secret_key`/`ws_token`/`password` 均为本地占位符，部署时务必替换。
真实 `.env`、`config.json`、`*.session` 已被 `.gitignore` 排除，请勿提交。
