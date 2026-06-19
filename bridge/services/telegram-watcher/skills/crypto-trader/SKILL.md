---
name: crypto-trader
description: 自动化加密货币合约交易 Skill。接收 Telegram 信号后分析、下单、追踪订单、生成晨报。当用户转发 Telegram 交易信号、询问持仓状态、或请求生成晨报时触发。
---

# Crypto Trader Skill

## Overview

自动化加密货币 USDT 合约交易系统。接收用户转发的 Telegram 消息后，自动分类为交易信号/订单更新/行情简报，执行仓位计算与下单，追踪活跃订单，并生成每日晨报。

## Quick Start

首次使用需初始化数据库、添加账户、配置频道路由和风险参数：

```bash
# 1. 初始化数据库
python3 scripts/db_manager.py init-db

# 2. 添加 Binance 账户
python3 scripts/db_manager.py add-account main "YOUR_API_KEY" "YOUR_API_SECRET" --risk 0.01

# 3. 设置频道路由（Telegram channel_id → Binance 账户）
python3 scripts/db_manager.py set-channel "channel_123" main --name "VIP信号群"

# 4. 设置品种风险比例
python3 scripts/db_manager.py set-risk BTCUSDT 0.02
python3 scripts/db_manager.py set-risk ETHUSDT 0.015
```

## 图片信号处理

信号消息可能以图片形式发送（如 K线截图、标注了入场/止损/止盈的图表）。当转发消息中包含图片附件时：

1. **识别图片：** 消息中会包含 `文件路径:` 和 `⚠️ 这是一张图片` 提示
2. **提取信息：** 使用 `image` 工具分析图片，**必须指定 model 参数为 `kimi-coding/k2p5`**，提取交易信号参数（币种、方向、入场价、止损、止盈等）
3. **结合文字：** 图片信号通常附带文字说明（caption），将图片提取结果与文字内容合并分析
4. **后续流程：** 提取出参数后，走正常的信号分类和下单流程

```
# 示例：用 image 工具提取图片中的交易信号（必须指定 model）
image(image="/path/to/media/1234567890-42.jpg", model="kimi-coding/k2p5", prompt="提取这张交易图表中的信号信息：交易对、方向(LONG/SHORT)、入场价、止损价、止盈价、杠杆倍数。以结构化格式输出。")
```

### 图片分析失败降级

如果 `image` 工具调用失败（返回错误、空内容、或超时），**不要放弃处理**，按以下降级策略继续：

1. **检查文字内容是否已包含完整信号参数**（入场价、止损、方向）
2. 如果文字内容足够 → 直接用文字参数下单，在通知中注明"⚠️ 图片分析失败，仅基于文字内容下单"
3. 如果文字内容不完整（缺少关键参数如止损价）→ 存为 PENDING 订单，通知用户补充缺失参数
4. **绝不因为图片分析失败就完全跳过信号处理**

**注意：** 如果图片内容模糊或无法提取完整参数，将已识别的信息存为 PENDING 订单，并通知用户补充缺失参数。

## Message Classification

收到用户转发的消息时，按以下决策树分类：

```
消息是否包含明确的入场价格和方向（LONG/SHORT 或 BUY/SELL）？
├── YES → 是否包含止损价（SL）？
│   ├── YES → 交易信号（自动执行）
│   └── NO  → 交易信号（存为 PENDING，提示用户补充 SL）
├── 消息是否引用已有持仓（如"移动止损到 XX"、"平仓"、"减仓"）？
│   └── YES → 订单更新
├── 消息是否为市场分析、观点、新闻？
│   └── YES → 行情简报（存入 briefings 表）
└── 以上都不是 → 无关消息（忽略，简短回复用户）
```

**分类关键词参考：**
- 交易信号：`entry`、`入场`、`开多/开空`、`LONG/SHORT`、`SL`、`TP`、具体价格数字
- 订单更新：`移动止损`、`move SL`、`平仓`、`close`、`减仓`、`partial close`
- 行情简报：`分析`、`看法`、`预计`、`走势`、`news`、`update`

## Workflow: 处理交易信号

### Step 0: 防重复检查（必须最先执行）

从消息中提取 signal_id（格式：`频道ID:消息ID`，如 `-1002198013097:3927`），频道ID 和消息ID 来自 watcher 转发的消息头部信息。

**交易信号入场前检查：**

```bash
python3 scripts/db_manager.py --db ~/projects/trading-data/trading.db check-signal "<signal_id>" entry
```

- 如果返回 `"exists": true` → **立即停止**，回复"该信号已处理，跳过"，不再执行后续步骤
- 如果返回 `"exists": false` → 继续执行 Step 1

**下单成功后记录：**

在 Step 5（执行下单）成功后，立即记录：

```bash
python3 scripts/db_manager.py --db ~/projects/trading-data/trading.db record-signal "<signal_id>" entry --symbol BTCUSDT --side SELL --detail '{"binance_order_id":"xxx","qty":0.015}'
```

如果 `record-signal` 返回 `"status": "duplicate"` → 说明另一个 session 已经抢先下单了，**取消刚下的单**，避免重复持仓。

**operation_type 类型说明：**

| operation_type | 用途 | 触发场景 |
|----------------|------|----------|
| `entry` | 开仓下单 | 处理交易信号时 |
| `move_sl` | 移动止损 | 处理订单更新（移动SL）时 |
| `partial_close` | 部分平仓 | 处理订单更新（减仓）时 |
| `full_close` | 全部平仓 | 处理订单更新（平仓）时 |
| `add_tp` | 添加/修改止盈 | 处理止盈价格更新时 |
| `briefing` | 行情简报 | 存储行情分析消息时 |

**订单更新的防重复：** 收到订单更新消息（移动止损、部分平仓等）时，同样先 `check-signal`，再执行，成功后 `record-signal`。signal_id 不变（同一条原始消息），但 operation_type 不同，所以不会冲突。

### Step 1: 识别信号参数

从消息中提取以下参数：

| 参数 | 必须 | 说明 |
|------|------|------|
| symbol | YES | 交易对，如 BTCUSDT、ETHUSDT |
| side | YES | 方向：BUY（做多）或 SELL（做空） |
| entry_price | YES | 入场价格（可能是区间或多个价位） |
| stop_loss | 推荐 | 止损价格，无则存为 PENDING |
| take_profit | 可选 | 止盈价格（可能有多档） |
| leverage | 可选 | 杠杆倍数，默认 10 |

**注意：** 信号中的 LONG = BUY，SHORT = SELL。

**多入场价格（DCA）：** 信号可能包含多个入场位（如"首次入场 31.22，定投入场 30.8"）。提取所有入场价位。

**多档止盈：** 信号可能包含多个 TP 目标（如"首次止盈 25%，第二次 50%..."）。如果信号只给了百分比没有具体价格，需要结合图表或估算。如果无法确定具体 TP 价格，只记录百分比比例，在通知中说明"止盈价格待定，需用户确认"。

### Step 2: 查找频道路由

根据消息来源的 channel_id 查找对应的 Binance 账户：

```bash
python3 scripts/db_manager.py list-channels
```

在返回的 JSON 中找到 `channel_id` 对应的 `target_account_id`。如果 channel_id 不在路由表中，提示用户配置。

### Step 3: 仓位计算

#### 3a. 获取风险比例

```bash
python3 scripts/db_manager.py get-risk BTCUSDT --account main
```

风险回退链：`symbol_risk_configs` → 账户 `default_risk_ratio` → 全局默认 0.01 (1%)。

#### 3b. 获取账户余额

```bash
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main get-balance
```

#### 3c. 计算仓位大小

```bash
python3 scripts/binance_trade.py calc-position --balance 10000 --risk-ratio 0.02 --entry 65000 --sl 64000
```

公式：`quantity = (balance * risk_ratio) / abs(entry_price - stop_loss)`

返回 JSON 包含 `quantity`、`risk_amount`、`distance`。

详细说明参见 `references/position-sizing.md`。

### Step 4: 下单决策

先用 `get-bookticker` 获取买1卖1，判断入场方式：

- **无 SL** → 先在数据库记录为 PENDING，然后提示用户补充 SL
- **有 SL + 当前价在入场区间内** → 用买1/卖1限价单入场
- **有 SL + 当前价不在入场区间** → 挂限价单等回调

判断"当前价是否在入场区间"：
- LONG 信号：当前价 ≤ 入场价上沿 → 当前价在区间，用买1价入场；当前价 > 入场价上沿 → 挂限价单
- SHORT 信号：当前价 ≥ 入场价下沿 → 当前价在区间，用卖1价入场；当前价 < 入场价下沿 → 挂限价单

**CMP（市价）入场处理：**
- 信号给出 "CMP" 或 "市价" 入场时，采用分批建仓策略：
  - **60%** 仓位：用**市价单**买入（当前买1价 bidPrice）
  - **40%** 仓位：分两批限价单
    - 20% 在中间价挂单：中间价 = (CMP + 挂单价) / 2
    - 20% 在挂单价挂单
- 示例：LONG，CMP=9.093，挂单价=8.80
  - 60% @ 9.093（市价）= 0.6 × quantity
  - 20% @ 8.95（中间价）= 0.2 × quantity
  - 20% @ 8.80（限价）= 0.2 × quantity
- 止损/止盈按总仓位计算，下单时用总数量

**多入场位（DCA）处理：**
- 信号有"首次入场"和"定投入场"两个价位 → 拆成 4 笔限价单
- 仓位分配规则：
  - 首次入场：总仓位的 **50%**（1 笔）
  - 定投入场：剩余 **50% 均分 3 笔**（每笔约 16.7%）
  - 定投 3 笔价格：围绕定投价上下浮动（如定投价 30.8 → 31.0 / 30.8 / 30.6）
- 用 SL 和首次入场价计算总仓位，再按上述比例分配
- 每笔入场单独立记录为一条 active_order（同一信号关联同一 channel_id）

无 SL 时提示用户：

```
信号已识别：BTCUSDT LONG @ 65000
未包含止损价，请提供 SL 价格以自动下单，或输入 "取消" 放弃。
```

### Step 5: 执行下单

**重要：不使用市价单，全部用限价单。** 市价单是 taker 手续费（贵），限价单是 maker 手续费（便宜）。

入场下单前，先获取买1卖1价格：

```bash
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main get-bookticker BTCUSDT
```

返回 `bidPrice`（买1）和 `askPrice`（卖1）。

**入场价格规则：**
- BUY（做多）→ 用 **买1价 bidPrice** 挂限价单（排队买入）
- SELL（做空）→ 用 **卖1价 askPrice** 挂限价单（排队卖出）
- 如果信号指定了入场区间且当前价不在区间内 → 用信号指定的入场价挂限价单

按以下顺序执行（每步检查返回 JSON 的 status）：

```bash
# 5a. 设置杠杆
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main set-leverage BTCUSDT 10

# 5b. 下入场单 — 限价单（用买1/卖1价格）
# 做多：用 bidPrice
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-order BTCUSDT BUY 0.015 --type LIMIT --price 65000

# 做空：用 askPrice
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-order BTCUSDT SELL 0.015 --type LIMIT --price 65000

# 5b-DCA. 多入场位 → 拆成 4 笔限价单
# 例：HYPE LONG，总仓位 10，首次入场 31.22，定投 30.8，SL 30.0
# 首次入场 50%（5.0）
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-order HYPEUSDT BUY 5.0 --type LIMIT --price 31.22
# 定投 1/3（~1.67）
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-order HYPEUSDT BUY 1.67 --type LIMIT --price 31.00
# 定投 2/3（~1.67）
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-order HYPEUSDT BUY 1.67 --type LIMIT --price 30.80
# 定投 3/3（~1.66）
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-order HYPEUSDT BUY 1.66 --type LIMIT --price 30.60

# 5c. 下止损单（方向与持仓相反，数量 = 所有入场单总量）
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-sl BTCUSDT SELL 0.015 64000

# 5d. 下止盈单 — 单档 TP
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-tp BTCUSDT SELL 0.015 68000

# 5d-multi. 下止盈单 — 多档 TP（分批止盈）
# 每档挂一个 TP 单，数量按百分比分配
# 例：总仓位 10，四档 25%/50%/75%/100%
# TP1 平 25%（2.5）
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-tp BTCUSDT SELL 2.5 39.0
# TP2 平 25%（再平总仓位的25%，剩余的1/3）
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-tp BTCUSDT SELL 2.5 46.8
# TP3 平 25%
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-tp BTCUSDT SELL 2.5 55.0
# TP4 平剩余全部
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-tp BTCUSDT SELL 2.5 62.0
```

**多档止盈分配规则：**
- 信号给的百分比是**累计**比例（25%→50%→75%→100%）
- 每档实际平仓量 = 总仓位 × 25%（本例每档都是 25%）
- 如果信号只给了百分比没有具体价格 → 通知用户"止盈价格待定，请提供具体 TP 价位"
- 如果有具体 TP 价格 → 直接挂所有 TP 单

**限价单特殊处理：**
- 入场单用 `--type LIMIT --price <bid1或ask1或信号入场价>`
- SL/TP 也要同时挂上
- 订单状态记录为 `PENDING_ENTRY`（限价单未成交时）或 `OPEN`（当前价在入场价附近，预期很快成交）

### Step 6: 记录订单

```bash
# 6a. 创建订单记录
python3 scripts/db_manager.py create-order "channel_123" main BTCUSDT BUY 65000 --sl 64000 --tp 68000 --qty 0.015

# 6b. 更新为 OPEN 状态，记录 Binance 订单号
python3 scripts/db_manager.py update-order <order_id> OPEN --binance-id "<binance_order_id>"
```

**向用户确认下单结果：**

```
已执行：BTCUSDT LONG
- 入场：65000（市价单已成交）
- 数量：0.015 BTC
- 止损：64000
- 止盈：68000
- 风险金额：$200（余额的 2%）
- 订单 ID：#<order_id>
```

### Step 7: 设置价格警报

下单完成后，**必须**为每个 TP 目标设置价格警报。watcher 会每 10 秒检查一次价格，到价时自动触发 trader agent 做订单管理。

通过 watcher API 添加警报（`http://127.0.0.1:9100`）：

```bash
# 做多订单的 TP 警报（方向 = above，价格上穿目标价触发）
curl -X POST http://127.0.0.1:9100/api/price-alerts -H "Content-Type: application/json" \
  -d '{"order_id": 5, "symbol": "BTCUSDT", "target_price": 68000, "direction": "above", "alert_type": "tp1", "quantity": 0.004, "note": "第一档止盈 25%"}'

curl -X POST http://127.0.0.1:9100/api/price-alerts -H "Content-Type: application/json" \
  -d '{"order_id": 5, "symbol": "BTCUSDT", "target_price": 72000, "direction": "above", "alert_type": "tp2", "quantity": 0.004, "note": "第二档止盈 50%"}'

# 做空订单的 TP 警报（方向 = below，价格下穿目标价触发）
curl -X POST http://127.0.0.1:9100/api/price-alerts -H "Content-Type: application/json" \
  -d '{"order_id": 6, "symbol": "BTCUSDT", "target_price": 62000, "direction": "below", "alert_type": "tp1", "quantity": 0.01, "note": "第一档止盈 25%"}'
```

**direction 规则：**
- LONG 持仓止盈 → `"above"`（价格涨到目标价触发）
- SHORT 持仓止盈 → `"below"`（价格跌到目标价触发）

**alert_type 类型：**
- `tp1`, `tp2`, `tp3`, `tp4` — 分档止盈
- `entry` — 入场价格提醒
- `custom` — 自定义价格提醒

**查看当前警报：**
```bash
curl http://127.0.0.1:9100/api/price-alerts?triggered=false
```

**删除警报（订单平仓后清理）：**
```bash
# 删除单个
curl -X DELETE http://127.0.0.1:9100/api/price-alerts/1
# 删除某订单的所有警报
curl -X DELETE http://127.0.0.1:9100/api/price-alerts/order/5
```

**到价触发后：** watcher 自动创建 cron job 通知 trader agent，agent 会收到包含订单 ID、当前价、目标价等信息的消息，按照订单管理流程执行（部分平仓/全部平仓/移动止损等）。触发后该警报自动标记为 triggered，不会重复触发。

## Workflow: 处理订单更新

收到订单更新消息时，先提取 signal_id 并做防重复检查：

```bash
# 移动止损场景
python3 scripts/db_manager.py --db ~/projects/trading-data/trading.db check-signal "<signal_id>" move_sl
# 部分平仓场景
python3 scripts/db_manager.py --db ~/projects/trading-data/trading.db check-signal "<signal_id>" partial_close
# 全部平仓场景
python3 scripts/db_manager.py --db ~/projects/trading-data/trading.db check-signal "<signal_id>" full_close
```

如果 `"exists": true` → 跳过，回复"该操作已处理"。
如果 `"exists": false` → 继续执行，完成后 `record-signal`。

然后查找对应的活跃订单：

```bash
python3 scripts/db_manager.py list-orders --status OPEN --channel "channel_123"
```

### 移动止损 (Move SL)

```bash
# 1. 取消旧止损单
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main cancel-order BTCUSDT <old_sl_order_id>

# 2. 下新止损单
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-sl BTCUSDT SELL 0.015 65500

# 3. 更新数据库记录
python3 scripts/db_manager.py update-sl <order_id> 65500 --sl-order-id "<new_sl_order_id>"
```

### 部分平仓 (Partial Close)

```bash
# 1. 查询当前持仓确认数量
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main get-position BTCUSDT

# 2. 下反向市价单（reduceOnly 由脚本自动处理不适用于市价单，需手动控制数量）
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-order BTCUSDT SELL 0.005

# 3. 更新 SL/TP 数量（取消旧单，以新数量重新挂单）
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main cancel-all BTCUSDT
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-sl BTCUSDT SELL 0.010 64000

# 4. 更新数据库状态
python3 scripts/db_manager.py update-order <order_id> PARTIAL_CLOSED
```

### 全部平仓 (Full Close)

```bash
# 1. 查询持仓数量
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main get-position BTCUSDT

# 2. 下反向市价单平仓
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main place-order BTCUSDT SELL 0.015

# 3. 取消该品种所有挂单（SL/TP）
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account main cancel-all BTCUSDT

# 4. 更新数据库状态为 CLOSED
python3 scripts/db_manager.py update-order <order_id> CLOSED
```

## Workflow: 行情分析消息

收到非交易信号的分析/观点/新闻消息时，存入 briefings 表供日报使用：

```bash
python3 scripts/db_manager.py add-briefing "<channel_id>" "<消息文本>" --category analysis
```

category 分类：
- `analysis` — 技术分析、观点（如"BTC 短期看涨，关注 68000 阻力位"）
- `news` — 市场新闻、政策动态
- `signal` — 非完整信号但提及了交易方向的消息
- `other` — 其他有价值的信息

## Workflow: 每日交易日报

每日早上 8:00 自动生成，通过 cron job 触发。

### 日报内容

1. **持仓概览** — 查询所有 OPEN 订单，获取当前价格和浮盈浮亏
2. **过去 24 小时信号回顾** — 收到了哪些信号，执行了哪些，跳过了哪些
3. **频道行情摘要** — 从 briefings 表汇总分析观点和新闻
4. **风险提示** — 当前总敞口、各品种风险分布

### 生成步骤

```bash
# 1. 获取晨报素材
python3 scripts/briefing.py --db ~/projects/trading-data/trading.db generate --hours 24

# 2. 查询当前持仓
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account jiataotx get-position BTCUSDT
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account jiataotx get-position ETHUSDT
# ... 对每个关注品种执行

# 3. 查询活跃订单
python3 scripts/db_manager.py --db ~/projects/trading-data/trading.db list-orders --status OPEN

# 4. 获取账户余额
python3 scripts/binance_trade.py --db ~/projects/trading-data/trading.db --account jiataotx get-balance
```

### 日报格式

```
📊 交易日报 — YYYY-MM-DD

💰 账户概览
余额：$XXXX | 总浮盈亏：$XX

📈 当前持仓
• BTCUSDT SHORT -0.035 @ 71028 | 浮盈 $XX | SL 72800
• ...

📋 昨日信号 (X 条)
• ✅ BTCUSDT SHORT — 已执行
• ⏳ SOLUSDT LONG — 限价单等待中
• ❌ ETHUSDT — 无 SL，未执行

📰 行情摘要
• [分析] BTC 短期看涨，关注 68000 阻力位
• [新闻] ...

⚠️ 风险提示
• 总敞口：$XXXX（占余额 XX%）
```

### 生成 Hexo 博客日报

数据收集完成后，组装成 JSON 并用 `report_renderer.py` 渲染成 Hexo MD 页面。持仓/挂单部分会调用 `report_image.py` 生成暗色主题 PNG 并自动嵌入。每天生成独立页面（`/blog/report/YYYY-MM-DD/`），归档页（`/blog/report/`）自动列出所有历史日报。

```bash
# 5. 组装 JSON 数据（agent 自行构造），然后渲染
echo '<json_data>' | python3 scripts/report_renderer.py
# 自动写入 ~/.openclaw/workspace/blog/source/report/YYYY-MM-DD.md
# 自动更新 ~/.openclaw/workspace/blog/source/report/index.md（归档页）
# 输出 {"status":"ok","url":"https://blog.balen.wang/blog/report/YYYY-MM-DD/","index":"https://blog.balen.wang/blog/report/"}

# 6. 确认 Hexo server 在运行（端口 8462），如没运行则启动
cd ~/.openclaw/workspace/blog && npx hexo server -p 8462 &
```

**report_renderer.py 输入 JSON 格式：**
```json
{
  "date": "2026-02-10",
  "generated_at": "2026-02-10 08:00:15",
  "account": {
    "balance": 1927.01,
    "margin_used": 156.80,
    "pnl": -12.35
  },
  "positions": [
    {"symbol": "ETHUSDT", "side": "SHORT", "qty": 0.319, "entry": 2164.0, "current": 2180.5, "sl": 2254.0, "pnl": -5.26}
  ],
  "pending_orders": [
    {"symbol": "TAOUSDT", "side": "SELL", "qty": 1.52, "price": 157.46}
  ],
  "signals_24h": [
    {"symbol": "ETHUSDT", "action": "SHORT @ 2164", "status": "executed", "time": "19:14"}
  ],
  "risk": {
    "total_exposure": 648,
    "exposure_pct": 33.6,
    "notes": ["ETH SHORT 接近止损区间"]
  }
}
```

**手动触发：** 用户发送 `/report` 或"生成日报"时，按上述流程实时生成并发送链接。

### 发送

日报生成后，发送到 Telegram：

```
message action=send channel=telegram accountId=trader target=6684959561
message="📊 交易日报 — 2026-02-10
余额 $1,927 | 持仓 2 | 信号 3

📄 日报：https://blog.balen.wang/blog/report/2026-02-10/
📁 归档：https://blog.balen.wang/blog/report/"
```

### 查询所有持仓（推荐方式）

不要逐个品种查，用 Python 一次性获取全部非零持仓：

```bash
cd /Users/balen/.openclaw/workspace-trader/skills/crypto-trader/scripts && python3 -c "
import sys, os, sqlite3
sys.path.insert(0, '.')
from binance_trade import BinanceTrader
db_path = os.path.expanduser('~/projects/trading-data/trading.db')
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute('SELECT api_key, api_secret, is_testnet FROM account_configs WHERE account_id = ?', ('jiataotx',))
row = cursor.fetchone()
conn.close()
trader = BinanceTrader(row[0], row[1], testnet=bool(row[2]))
positions = trader.client.futures_position_information()
for p in positions:
    amt = float(p['positionAmt'])
    if amt != 0:
        print(f\"{p['symbol']} {p['positionSide']} amt={p['positionAmt']} entry={p['entryPrice']} mark={p['markPrice']} pnl={p['unRealizedProfit']}\")
"
```

## Script Reference

### db_manager.py — 数据库管理

| 子命令 | 用途 | 示例 |
|--------|------|------|
| `init-db` | 初始化数据库表 | `python3 scripts/db_manager.py init-db` |
| `add-account` | 添加 Binance 账户 | `python3 scripts/db_manager.py add-account main KEY SECRET --risk 0.01 --testnet` |
| `list-accounts` | 列出所有账户 | `python3 scripts/db_manager.py list-accounts` |
| `set-channel` | 设置频道路由 | `python3 scripts/db_manager.py set-channel ch_123 main --name "VIP群"` |
| `list-channels` | 列出所有频道路由 | `python3 scripts/db_manager.py list-channels` |
| `set-risk` | 设置品种风险比例 | `python3 scripts/db_manager.py set-risk BTCUSDT 0.02` |
| `list-risks` | 列出所有风险配置 | `python3 scripts/db_manager.py list-risks` |
| `get-risk` | 查询品种风险比例 | `python3 scripts/db_manager.py get-risk BTCUSDT --account main` |
| `create-order` | 创建订单记录 | `python3 scripts/db_manager.py create-order ch_123 main BTCUSDT BUY 65000 --sl 64000 --tp 68000 --qty 0.015` |
| `update-order` | 更新订单状态 | `python3 scripts/db_manager.py update-order 1 OPEN --binance-id "12345"` |
| `update-sl` | 更新止损价 | `python3 scripts/db_manager.py update-sl 1 65500 --sl-order-id "67890"` |
| `list-orders` | 列出订单 | `python3 scripts/db_manager.py list-orders --status OPEN --channel ch_123` |
| `add-briefing` | 添加晨报素材 | `python3 scripts/db_manager.py add-briefing ch_123 "BTC看涨" --category analysis` |
| `list-briefings` | 列出近期素材 | `python3 scripts/db_manager.py list-briefings --hours 24` |
| `check-signal` | 检查信号操作是否已存在 | `python3 scripts/db_manager.py check-signal "-1002198013097:3927" entry` |
| `record-signal` | 记录信号操作（幂等） | `python3 scripts/db_manager.py record-signal "-1002198013097:3927" entry --symbol BTCUSDT --side SELL --detail '{}'` |
| `list-signal-ops` | 列出信号操作记录 | `python3 scripts/db_manager.py list-signal-ops --signal-id "-1002198013097:3927"` |

所有子命令支持 `--db PATH` 参数，默认 `~/projects/trading-data/trading.db`。

### binance_trade.py — Binance 合约交易

| 子命令 | 用途 | 需要凭据 | 示例 |
|--------|------|----------|------|
| `get-price` | 查询当前价格 | YES | `python3 scripts/binance_trade.py --db ... --account main get-price BTCUSDT` |
| `get-bookticker` | 查询买1卖1价格 | YES | `python3 scripts/binance_trade.py --db ... --account main get-bookticker BTCUSDT` |
| `get-balance` | 查询账户余额 | YES | `python3 scripts/binance_trade.py --db ... --account main get-balance` |
| `calc-position` | 计算仓位大小 | NO | `python3 scripts/binance_trade.py calc-position --balance 10000 --risk-ratio 0.02 --entry 65000 --sl 64000` |
| `place-order` | 下单 | YES | `python3 scripts/binance_trade.py --db ... --account main place-order BTCUSDT BUY 0.015` |
| `place-sl` | 下止损单 | YES | `python3 scripts/binance_trade.py --db ... --account main place-sl BTCUSDT SELL 0.015 64000` |
| `place-tp` | 下止盈单 | YES | `python3 scripts/binance_trade.py --db ... --account main place-tp BTCUSDT SELL 0.015 68000` |
| `cancel-order` | 取消指定订单 | YES | `python3 scripts/binance_trade.py --db ... --account main cancel-order BTCUSDT 12345` |
| `cancel-all` | 取消品种所有挂单 | YES | `python3 scripts/binance_trade.py --db ... --account main cancel-all BTCUSDT` |
| `get-position` | 查询当前持仓 | YES | `python3 scripts/binance_trade.py --db ... --account main get-position BTCUSDT` |
| `get-orders` | 查询所有挂单 | YES | `python3 scripts/binance_trade.py --db ... --account main get-orders BTCUSDT` |
| `set-leverage` | 设置杠杆 | YES | `python3 scripts/binance_trade.py --db ... --account main set-leverage BTCUSDT 10` |

凭据加载：`--db PATH --account ACCOUNT_ID` 从数据库读取。`--db` 默认为空，需显式传入。

### briefing.py — 晨报生成

| 子命令 | 用途 | 示例 |
|--------|------|------|
| `generate` | 生成 Markdown 晨报 | `python3 scripts/briefing.py --db ~/projects/trading-data/trading.db generate --hours 24` |
| `list` | 列出晨报素材 | `python3 scripts/briefing.py --db ~/projects/trading-data/trading.db list --hours 48` |
| `stats` | 晨报统计信息 | `python3 scripts/briefing.py --db ~/projects/trading-data/trading.db stats --hours 24` |

## Important Rules

1. **NEVER** 在未确认账户余额充足的情况下下单。先 `get-balance`，再计算。
2. **ALWAYS** 在下单前设置杠杆（`set-leverage`）。
3. **ALWAYS** SL/TP 使用 reduceOnly（`place-sl` 和 `place-tp` 已自动处理）。
4. SL/TP 方向必须与持仓方向**相反**：
   - LONG 持仓：SL side = SELL，TP side = SELL
   - SHORT 持仓：SL side = BUY，TP side = BUY
5. 所有脚本输出 JSON，解析 `status` 字段判断执行结果。出错时 `status` 为 `"error"`。
6. 默认数据库路径：`~/projects/trading-data/trading.db`。
7. 脚本路径前缀：`scripts/`。
8. 订单状态流转：`PENDING` → `OPEN` → `PARTIAL_CLOSED` / `CLOSED` / `CANCELLED`。
9. 同一频道的后续消息应关联到该频道已有的 OPEN 订单，通过 `--channel` 过滤。
10. 仓位计算详细说明参见 `references/position-sizing.md`。
11. 信号解析示例参见 `references/signal-examples.md`。
12. **通知规则：** 所有通知通过 `message` 工具发送，参数固定为 `action=send channel=telegram accountId=trader target=6684959561`。每次处理信号发两条：①收到信号确认 ②处理结果。
13. **限价单：** 当前价不在入场区间时，用 `place-order --type LIMIT --price <price>` 挂限价单，订单状态记为 `PENDING_ENTRY`。
14. **Algo 订单：** 测试网的 SL/TP 是 algo/conditional 订单，用 `get-orders` 可以同时查到普通订单和 algo 订单。取消 algo 订单需用 `cancel-order` 传入 algoId。
15. **行情分析：** 非交易信号的分析消息用 `add-briefing` 存入数据库，供日报使用。
