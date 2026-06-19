# Position Sizing — 固定风险仓位计算模型

## 核心公式

```
quantity = (balance × risk_ratio) / abs(entry_price - stop_loss)
```

| 变量 | 含义 | 来源 |
|------|------|------|
| `balance` | 账户 USDT 可用余额 | `binance_trade.py get-balance` |
| `risk_ratio` | 单笔最大亏损占余额比例 | `db_manager.py get-risk` |
| `entry_price` | 计划入场价格 | 信号消息 |
| `stop_loss` | 止损价格 | 信号消息 |
| `quantity` | 计算出的开仓数量（币本位） | 输出 |

**含义：** 如果价格从 entry 移动到 SL 被止损，亏损金额恰好等于 `balance × risk_ratio`。

## 风险比例回退链

`get-risk` 命令按以下优先级查找风险比例：

```
1. symbol_risk_configs 表（品种级别）
   ↓ 未找到
2. account_configs.default_risk_ratio（账户级别）
   ↓ 未找到
3. 全局默认 0.01（1%）
```

查询命令：

```bash
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py get-risk BTCUSDT --account main
```

返回 JSON 中的 `source` 字段标明来源：`symbol_config`、`account_default` 或 `global_default`。

## 推荐风险配置

| 品种 | risk_ratio | 说明 |
|------|-----------|------|
| BTCUSDT | 0.02 (2%) | 主流品种，波动相对可控 |
| ETHUSDT | 0.015 (1.5%) | 主流品种，波动略大于 BTC |
| 山寨币默认 | 0.01 (1%) | 波动大，用全局默认即可 |

配置命令：

```bash
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py set-risk BTCUSDT 0.02
python3 ~/.claude/skills/crypto-trader/scripts/db_manager.py set-risk ETHUSDT 0.015
```

## 计算示例

### 示例 1：BTC 做多，2% 风险

**场景：** 余额 $10,000，BTCUSDT LONG @ 65000，SL @ 64000

```bash
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py calc-position \
  --balance 10000 --risk-ratio 0.02 --entry 65000 --sl 64000
```

**计算过程：**

```
risk_amount = 10000 × 0.02 = $200
distance    = |65000 - 64000| = 1000
quantity    = 200 / 1000 = 0.2 BTC
```

**返回：**

```json
{"quantity": 0.2, "risk_amount": 200.0, "distance": 1000.0}
```

**验证：** 如果 BTC 从 65000 跌到 64000（跌 1000），持有 0.2 BTC 的亏损 = 0.2 × 1000 = $200 = 余额的 2%。

### 示例 2：ETH 做空，1.5% 风险

**场景：** 余额 $5,000，ETHUSDT SHORT @ 3200，SL @ 3300

```bash
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py calc-position \
  --balance 5000 --risk-ratio 0.015 --entry 3200 --sl 3300
```

**计算过程：**

```
risk_amount = 5000 × 0.015 = $75
distance    = |3200 - 3300| = 100
quantity    = 75 / 100 = 0.75 ETH
```

**返回：**

```json
{"quantity": 0.75, "risk_amount": 75.0, "distance": 100.0}
```

### 示例 3：山寨币做多，默认 1% 风险

**场景：** 余额 $8,000，SOLUSDT LONG @ 150，SL @ 142

```bash
python3 ~/.claude/skills/crypto-trader/scripts/binance_trade.py calc-position \
  --balance 8000 --risk-ratio 0.01 --entry 150 --sl 142
```

**计算过程：**

```
risk_amount = 8000 × 0.01 = $80
distance    = |150 - 142| = 8
quantity    = 80 / 8 = 10 SOL
```

**返回：**

```json
{"quantity": 10.0, "risk_amount": 80.0, "distance": 8.0}
```

## 杠杆与仓位的关系

杠杆影响保证金占用，但**不影响仓位计算公式**。风险模型关注的是止损时的绝对亏损金额，与杠杆无关。

**杠杆的作用：**

- 杠杆决定了开仓需要多少保证金：`margin = (quantity × entry_price) / leverage`
- 杠杆越高，保证金越少，但亏损金额不变
- 杠杆过高可能导致强平价格比止损价更近，应避免

**示例（接示例 1）：**

```
仓位价值 = 0.2 × 65000 = $13,000

10x 杠杆：margin = 13000 / 10 = $1,300（占余额 13%）
20x 杠杆：margin = 13000 / 20 = $650 （占余额 6.5%）
```

两种杠杆下，止损亏损都是 $200。区别在于保证金占用和强平距离。

**建议：** 使用 10x-20x 杠杆即可。确保保证金占用不超过余额的 30%。

## 边界情况

- **entry_price == stop_loss：** 公式返回 `quantity = 0`，防止除零错误。不应下单。
- **SL 距离过小：** 会导致仓位过大。如果计算出的仓位价值超过余额的 50%，应提醒用户风险过高。
- **SL 方向校验：** LONG 仓位的 SL 应低于 entry，SHORT 仓位的 SL 应高于 entry。如果方向不对，提示用户确认。
