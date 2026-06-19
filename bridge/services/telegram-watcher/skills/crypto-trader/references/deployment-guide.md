# 自动化交易系统 — 部署与使用指南

> 本文档面向 Agent（Claude）操作，包含完整的部署步骤和日常使用流程。

## 1. 系统概览

```
┌─────────────────────────────────────────────────────────┐
│                   远程设备 (192.168.31.95)                │
│                                                         │
│  telegram-watcher (Node.js, port 9100)                  │
│  ├── 监听 Telegram 频道消息                               │
│  ├── Web 管理界面（账号/频道/风控/订单）                    │
│  └── 消息存入 SQLite                                     │
│                                                         │
│  crypto-trader Skill (~/.claude/skills/crypto-trader/)   │
│  ├── db_manager.py    — 数据库管理                       │
│  ├── binance_trade.py — 币安合约交易                     │
│  └── briefing.py      — 晨报生成                        │
│                                                         │
│  SQLite DB: ~/projects/trading-data/trading.db           │
└─────────────────────────────────────────────────────────┘
```

## 2. 部署步骤

### 2.1 安装依赖

```bash
# Python 依赖
pip3 install python-binance

# Node.js（如未安装）
brew install node

# telegram-watcher 依赖
cd ~/projects/telegram-watcher
npm install
```

### 2.2 初始化数据库

```bash
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py init-db
```

验证：应输出 `{"status": "ok", "message": "Database initialized", "path": "..."}`

### 2.3 配置账号和频道

**方式 A：命令行**

```bash
# 添加 Binance 账号（testnet）
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py add-account main "YOUR_API_KEY" "YOUR_API_SECRET" --risk 0.01 --testnet

# 设置频道路由
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py set-channel "-1002198013097" main --name "VIP信号群"

# 设置币种风险比例
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py set-risk BTCUSDT 0.02
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py set-risk ETHUSDT 0.015
```

**方式 B：Web 管理界面**

启动 telegram-watcher 后访问 `http://localhost:9100`，在「账号」「频道路由」「风控」Tab 中操作。

### 2.4 启动 telegram-watcher

```bash
cd ~/projects/telegram-watcher
node server.js
# 或后台运行：
nohup node server.js > watcher.log 2>&1 &
```

首次启动需要在 `http://localhost:9100` 上配置 Telegram API 凭据并登录。

### 2.5 验证

```bash
# 检查数据库
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py list-accounts
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py list-channels
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py list-risks

# 检查 Binance 连接（需要已配置账号）
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main get-balance

# 检查 telegram-watcher
curl http://localhost:9100/api/status
```

---

## 3. 日常使用流程

### 3.1 处理交易信号

当用户转发 Telegram 消息给你时：

**Step 1：分类消息**

```
消息包含入场价+方向？ → 交易信号
引用已有持仓？       → 订单更新
市场分析/新闻？       → 存为晨报素材
都不是？             → 忽略
```

**Step 2：交易信号处理**

```bash
# 1. 查找频道路由
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py list-channels

# 2. 获取风险比例
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py get-risk BTCUSDT --account main

# 3. 获取余额
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main get-balance

# 4. 计算仓位
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py calc-position --balance 10000 --risk-ratio 0.02 --entry 65000 --sl 64000

# 5. 设置杠杆
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main set-leverage BTCUSDT 10

# 6. 下单
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-order BTCUSDT BUY 0.015

# 7. 挂止损（方向与持仓相反）
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-sl BTCUSDT SELL 0.015 64000

# 8. 挂止盈（可选）
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-tp BTCUSDT SELL 0.015 68000

# 9. 记录订单
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py create-order "-1002198013097" main BTCUSDT BUY 65000 --sl 64000 --tp 68000 --qty 0.015
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py update-order <order_id> OPEN --binance-id "<binance_order_id>"
```

**如果信号缺少止损价：**
- 先存为 PENDING：`create-order ... --qty 0`（不填 --sl）
- 提示用户补充止损价
- 用户确认后再执行上述流程

### 3.2 处理订单更新

```bash
# 查找活跃订单
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py list-orders --status OPEN --channel "-1002198013097"
```

**移动止损：**
```bash
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main cancel-order BTCUSDT <old_sl_order_id>
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-sl BTCUSDT SELL 0.015 65500
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py update-sl <order_id> 65500 --sl-order-id "<new_sl_order_id>"
```

**全部平仓：**
```bash
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main get-position BTCUSDT
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-order BTCUSDT SELL 0.015
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main cancel-all BTCUSDT
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py update-order <order_id> CLOSED
```

### 3.3 存储晨报素材

```bash
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py add-briefing "-1002198013097" "BTC 短期看涨，关注 68000 阻力位" --category analysis
```

category 可选：`analysis`（分析观点）、`news`（市场动态）、`signal`（交易信号提及）、`other`

### 3.4 生成晨报

```bash
python3 ~/.claude/skills/crypto-trader/scripts/briefing.py --db ~/projects/trading-data/trading.db generate --hours 24
```

---

## 4. 关键规则

1. **NEVER** 在未确认余额的情况下下单
2. **ALWAYS** 下单前先 `set-leverage`
3. SL/TP 方向必须与持仓**相反**：LONG → SL/TP 用 SELL，SHORT → SL/TP 用 BUY
4. 所有脚本输出 JSON，检查 `status` 字段判断成功/失败
5. 仓位公式：`qty = (balance × risk_ratio) / abs(entry - sl)`
6. 数据库路径：`~/projects/trading-data/trading.db`
7. 脚本路径：`~/.claude/skills/crypto-trader/scripts/`

## 5. 故障排查

| 问题 | 排查 |
|---|---|
| 脚本报 `No module named 'binance'` | `pip3 install python-binance` |
| 数据库报错 `no such table` | `python3 scripts/db_manager.py init-db` |
| Binance 报 `APIError(code=-2015)` | API Key 无效或权限不足 |
| telegram-watcher 无法启动 | 检查 Node.js 是否安装，`npm install` 是否执行 |
| 管理界面打不开 | 确认 `node server.js` 正在运行，访问 `http://localhost:9100` |
