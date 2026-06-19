# Signal Examples — Telegram 信号解析参考

本文档提供常见 Telegram 交易信号的解析示例，帮助准确提取交易参数并正确分类消息。

---

## 交易信号示例

### 示例 1：完整信号（英文，含 SL + TP）

**原始消息：**

```
BTCUSDT LONG
Entry: 65000
SL: 64000
TP1: 66500
TP2: 68000
Leverage: 10x
```

**提取结果：**

| 参数 | 值 |
|------|-----|
| symbol | BTCUSDT |
| side | BUY |
| entry_price | 65000 |
| stop_loss | 64000 |
| take_profit | 66500（取 TP1，或多个 TP 时取最近的） |
| leverage | 10 |

**分类：** 交易信号（自动执行）— 包含 entry + SL，直接下单。

---

### 示例 2：完整信号（中文格式）

**原始消息：**

```
以太坊开空
入场价：3200
止损：3300
目标：3000
```

**提取结果：**

| 参数 | 值 |
|------|-----|
| symbol | ETHUSDT |
| side | SELL |
| entry_price | 3200 |
| stop_loss | 3300 |
| take_profit | 3000 |
| leverage | 10（默认） |

**分类：** 交易信号（自动执行）。

**注意：** "以太坊" 需映射为 ETHUSDT，"开空" 对应 SELL。

---

### 示例 3：缺少止损的信号

**原始消息：**

```
SOL looking good here
Long at 150
Target 180
```

**提取结果：**

| 参数 | 值 |
|------|-----|
| symbol | SOLUSDT |
| side | BUY |
| entry_price | 150 |
| stop_loss | -- (缺失) |
| take_profit | 180 |

**分类：** 交易信号（PENDING）— 缺少 SL，存入数据库为 PENDING，提示用户补充。

**操作：**

```bash
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py create-order "channel_123" main SOLUSDT BUY 150 --tp 180
```

然后提示用户：

```
信号已识别：SOLUSDT LONG @ 150，目标 180
未包含止损价，请提供 SL 价格以自动下单。
```

---

### 示例 4：区间入场信号

**原始消息：**

```
BTC/USDT
SHORT 67500-68000
SL 69000
TP 65000 / 63000
```

**提取结果：**

| 参数 | 值 |
|------|-----|
| symbol | BTCUSDT |
| side | SELL |
| entry_price | 67750（取区间中点） |
| stop_loss | 69000 |
| take_profit | 65000（取最近的 TP） |

**分类：** 交易信号（自动执行）。

**注意：** 区间入场取中间值。多个 TP 取第一个（最保守）。

---

### 示例 5：简洁格式信号

**原始消息：**

```
DOGE long 0.38 sl 0.35 tp 0.45
```

**提取结果：**

| 参数 | 值 |
|------|-----|
| symbol | DOGEUSDT |
| side | BUY |
| entry_price | 0.38 |
| stop_loss | 0.35 |
| take_profit | 0.45 |

**分类：** 交易信号（自动执行）。

---

### 示例 6：限价单信号

**原始消息：**

```
AVAX limit buy at 32.5
SL at 30
TP 38
Wait for the pullback
```

**提取结果：**

| 参数 | 值 |
|------|-----|
| symbol | AVAXUSDT |
| side | BUY |
| entry_price | 32.5 |
| stop_loss | 30 |
| take_profit | 38 |
| order_type | LIMIT |

**分类：** 交易信号（自动执行，使用限价单）。

**注意：** 出现 "limit" 关键词时使用 `place-order --type LIMIT --price 32.5`。

---

## 订单更新示例

### 示例 7：移动止损

**原始消息：**

```
BTC 止损移到入场价 65000 保本
```

**操作：** 在 `active_orders` 中查找该频道 BTCUSDT 的 OPEN 订单，执行移动止损流程。

**分类：** 订单更新 — 移动止损。

---

### 示例 8：部分平仓

**原始消息：**

```
ETH close 50%
Take partial profits here
```

**操作：** 查找 ETHUSDT OPEN 订单，计算 50% 数量，执行部分平仓。

**分类：** 订单更新 — 部分平仓。

---

### 示例 9：全部平仓

**原始消息：**

```
Close all SOL positions
Market structure broken
```

**操作：** 查找 SOLUSDT OPEN 订单，执行全部平仓。

**分类：** 订单更新 — 全部平仓。

---

## 行情简报示例

### 示例 10：市场分析

**原始消息：**

```
BTC 日线收了一根十字星，短期方向不明
如果跌破 64000 支撑位可能会继续下探到 62000
上方阻力位在 68000-69000
观望为主
```

**操作：**

```bash
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py add-briefing "channel_123" "BTC日线十字星，方向不明。支撑64000（破则看62000），阻力68000-69000。建议观望。" --category analysis
```

**分类：** 行情简报（analysis）— 有分析观点但无明确入场点。

---

### 示例 11：新闻动态

**原始消息：**

```
Breaking: Fed holds rates steady, signals potential cut in Q2
Risk assets rallying across the board
```

**操作：**

```bash
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py add-briefing "channel_123" "美联储维持利率不变，暗示Q2可能降息。风险资产全线上涨。" --category news
```

**分类：** 行情简报（news）— 宏观新闻，非交易信号。

---

## 无关消息示例

### 示例 12：闲聊

**原始消息：**

```
大家周末愉快！
有人用过XX交易所吗？感觉还不错
```

**分类：** 无关消息 — 不含任何交易、分析内容，忽略。

---

## 常见品种名称映射

信号中可能使用非标准名称，需映射为 Binance 合约交易对：

| 信号中的写法 | 标准 symbol |
|-------------|------------|
| BTC, Bitcoin, 比特币 | BTCUSDT |
| ETH, Ethereum, 以太坊 | ETHUSDT |
| SOL, Solana | SOLUSDT |
| DOGE, Dogecoin, 狗狗币 | DOGEUSDT |
| AVAX, Avalanche | AVAXUSDT |
| BNB, 币安币 | BNBUSDT |
| XRP, Ripple, 瑞波 | XRPUSDT |
| ADA, Cardano | ADAUSDT |
| LINK, Chainlink | LINKUSDT |
| DOT, Polkadot, 波卡 | DOTUSDT |

**规则：** 统一追加 `USDT` 后缀，全部大写。如不确定，用 `get-price` 验证交易对是否存在。
