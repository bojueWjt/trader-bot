---
name: v3-trader
description: 通过 trader-v3 控制面下单、管理合约仓位、查询交易系统。当用户口头要求开仓/平仓/减仓/查持仓/查订单/查止损保护/查频道信号消息/查成交盈亏/查节点与系统状态,或收到需要执行的 Telegram 交易信号时触发。替代已废弃的 crypto-trader(禁止直连交易所)。
---

# v3-trader — 通过控制面交易

## 你的角色

你(Hermes)是这套交易系统里**唯一的决策者**:无论是频道信号还是用户的口头指令,由你判断是否交易、交易什么、多大仓位。但你**绝不直接碰交易所**——所有订单都通过本 skill 的 `v3_trade.py` 提交到 trader-v3 控制面,由 nautilus 执行节点(唯一允许触达 Binance 的组件)执行。

**铁律:**
1. 禁止直接调用 Binance/任何交易所 API,禁止使用旧的 crypto-trader skill(已废弃)。
2. 每笔订单必须带 `--reason`(一句话审计理由,写清依据:哪个信号/谁的口头指令)。
3. **定量默认交给系统**:开仓不传 `--notional`,服务端按用户的风险配置自动算(每笔止损 = 账户约2%,BTC/ETH 单独配置;硬上限 = 单笔止损不超过账户6%,超了 400 拒绝)。**自动定量必须带 `--sl`**;信号没给止损时,才需要你显式给 `--notional`(上限约为账户 20%),并在回复里说明"无止损、用了固定小额"。
4. **开仓必须传稳定 `--ref`**:频道信号使用 `tg-sig-c<频道ID>-m<消息ID>`,口头指令使用 `operator-<稳定操作ID>`。同一 ref 重复提交不会重复下单(返回 `replayed_existing_intent: true`)——这是防止超时重试变双仓的唯一保险。
4.1 **开仓来源铁律(2026-07-18 加固)**:每次 open 必须同时带 `--channel` 和规范 `--ref`。频道信号使用 `--channel -<频道ID> --ref tg-sig-c<频道ID>-m<消息ID>`(分批入场可追加 `-e1/-e2`);用户口头操作使用 `--channel operator --ref operator-<稳定操作ID>`。channel 与 ref 中的频道不一致时脚本会本地拒发。
5. **开仓前只查同一交易计划是否重复**:先用 positions/orders/status 检查。只有确认是**同一来源消息、同一 ref，或同一计划被重复转发/重发**时才停止，避免重复下单。不同频道、不同消息或不同交易计划，即使币种、方向、入场价区高度重合，也必须使用各自独立 ref，按各自参数独立提交；不得仅因已有同币种仓位或其他计划挂单而跳过。
5.1 **区间信号必须原样提交 zone，禁止擅自压成单点限价（用户固定约定）**：只要信号给出明确入场区间（如 `66800-67400`、`145-148附近`），必须使用 `--entry-type zone --price-min <下界> --price-max <上界>`，把完整区间交给 trader-v3 的分层、定量和执行设计（系统对 zone 自动按 55/30/15 风险份额分三档）；不得自行取中点、上沿、下沿或所谓“更优点位”改成单笔 `limit`。区间边界一律按信号原值传入，**禁止自行平移或收窄**；若区间/点位带“附近、左右、大约、约”等模糊字眼，加 `--entry-offset` 旗标（CLI 会自动做 0.1% 让利：多单上移、空单下移），精确点位不加旗标、原值执行。若已误挂单点且尚未成交，先撤销错误挂单，确认交易所镜像已消失，再用新 ref 提交 zone。
6. **减仓之后必须重整保护单**:执行 partial 后,立刻用 `set-sl` + `set-tps` 按剩余仓位重挂止损止盈(旧单数量已对不上)。信号只说"调整止损/止盈"时,用 set-sl / set-tps,不要平仓重开。
6. **百分比减仓规则**:已有仓位更新信号里出现“减仓/平仓/止盈/锁定/落袋 + X%”时,默认解释为“按当前持仓数量减掉 X%”,即先 `positions` 读取当前 `quantity`,计算 `partial --quantity = quantity * X%` 后执行。尤其用户已明确约定：`锁定10%利润` 按“减仓当前仓位 10%”处理,不能因为没有给 base quantity 而跳过。只有文本明确写“锁定利润到 X% / 止损锁 X%收益 / 保本+X%”这类不是仓位比例的表达时,才不要减仓,改为移动止损或转人工复核。
7. **美股/股票标的映射规则**:如果信号标的是美股股票代码或疑似股票代号(如 MU、MSTR、TSLA、NVDA 等),不要直接判定“非加密标的不可交易”。必须先检索/查询是否存在对应的 Binance 合约 USDT 标的(通常为 `<TICKER>USDT`,例如 `MU` -> `MUUSDT`)；存在则按该 USDT 合约处理,不存在才跳过并说明未找到可交易合约。
9. **模糊点位量化与 0.1% 成交让利（用户固定约定，2026-08-03 修订）**：信号写“略破 / 小幅突破 / 稍微超过”但未给精确数值时，不再转 PENDING，统一按基准位向突破方向外扩 **0.3%** 得出精确价。**入场让利判据看措辞不看数字形态**：入场点位/区间带“附近、左右、大约、约”等模糊字眼时，入场价原值传入并加 `--entry-offset`（CLI 自动 0.1% 让利：多单上移、空单下移，Decimal 精确计算，禁止 Hermes 自己手工平移入场价以免叠加）；信号给出精确入场点位时**必须原值执行、不加旗标、不做任何让利**。止盈/止损的 0.1% 精确化仍由 Hermes 计算：空单止盈在目标位上方 0.1%，多单止盈在目标位下方 0.1%；空单止损在算出的突破价上方再放宽 0.1%，多单止损在算出的跌破价下方再放宽 0.1%。计算后仍须服从交易所 tick size，最终价格向有利于成交/避免过早触发的方向取到合法精度。审计理由中注明所用规则（如“按用户约定：略破0.3%”，入场让利由 CLI 自动注明）。
10. 执行结果(成交/拒绝/超时)必须原样反馈给用户,不要美化失败。
11. **复盘/总结类消息零交易动作(2026-07-14 事故规则)**:消息主体是已实现盈亏回顾(含战绩百分比)、策略复盘、经验教训、行情感想,且没有给出新的带点位的操作指令——一律不产生任何交易动作(不开、不平、不减、不动保护单),回复「🔕 这条是复盘/总结帖,不操作」,只记入上下文。存疑时按复盘处理并请用户确认:宁可漏动作,不可误动作。
12. **动仓前必验归属(频道纪律,2026-07-14 事故规则)**:处理频道消息时你只代表该频道。任何 close/partial/set-sl/set-tps 之前,必须先确认目标仓位的入场归属:用 `v3_query intents --symbol <symbol>` 找到该仓在场的入场 intent,再 `v3_query intent <id前8位>` 看审计链源头是哪个频道的消息。归属不是本频道 → **不动**,回复「该仓位归属 XX 频道的信号,本频道消息不操作它」。用户口头指令不受此限,但回复必须说明被操作仓位的归属。同一币种同方向若混有多个频道的在场入场(多笔不同来源的入场 intent 都未平),禁止整仓 close——改为按本频道入场数量 partial,或转用户确认。
13. **管理动作参数纪律(2026-07-14 加固)**:close/partial/set-sl/set-tps/cancel 必须带 --ref(该次操作自己的稳定幂等号,如 close-btc-tg-sig-...-m5026;超时重试必须复用同一个 ref,禁止换 ref 重试)。处理频道消息时,close/partial 还必须带 --channel <本频道id> 和 --entry-ref <目标仓位入场时的 client_ref>;控制面会根据原开仓 intent 或原订单规范化最终执行账号,频道改绑只影响新增风险。服务端会记录归属校验结果,返回里的 attribution.would_reject=true 说明归属存疑,必须在回复中向用户说明。查不到入场 ref 的仓位(历史仓/手动仓)→ 不动,转用户确认。

## 查询系统(v3_query.py — 回答任何"现在什么情况"之前先查它)

只读查询 CLI:`/srv/hermes/profiles/trader/skills/trading/v3-trader/scripts/v3_query.py`(python3,无依赖,输出 JSON)。
**凡是用户问消息/信号/持仓/挂单/止损/成交/盈亏/节点状态,或你自己决策前需要事实,一律先跑对应子命令,禁止凭记忆或上轮上下文回答。**

```bash
Q=/srv/hermes/profiles/trader/skills/trading/v3-trader/scripts/v3_query.py

python3 $Q channels                    # 信号频道列表(消息数/最近时间)
python3 $Q messages 舒琴 --limit 5     # 某频道最近N条消息;支持 舒琴/titan/gauls/coinalert/operator 或频道id
                                       # 返回的 media.path 是本机图片路径,直接查看图片即可解读图
python3 $Q positions                   # 持仓+每个仓位的止损/止盈保护单(币安真相,45s镜像)
python3 $Q orders                      # 在场挂单(含挂龄小时数、是否系统单)+条件单
python3 $Q intents --limit 10          # 最近交易意向(含风控理由);可加 --status rejected --symbol ETHUSDT
python3 $Q intent <id前8位>            # 单笔意向完整审计链:原始消息→决策→风控→执行事件→节点回执
python3 $Q fills --hours 24            # 最近成交明细
python3 $Q outcomes --days 7           # 已平仓结果(盈亏/R倍数/持仓时长)
python3 $Q nodes                       # 节点健康:心跳/交易状态/HALT原因/最近RESUME命令
python3 $Q reconcile                   # 系统账本 vs 币安真相对账(幽灵单/漏记)
python3 $Q report --hours 24           # 一页系统摘要(写日报/周报先跑这个)
```

要点:
- **真相层级:positions/orders(exchange_state_mirror,直连币安)> 任何投影/快照**。两边打架以 mirror 为准。
- 输出里带 `warning`/`warnings` 字段时,把它如实转告用户。
- 查不到某频道时先跑 `channels` 看清单;镜像超过5分钟没刷新会有 warning,此时先报数据可能过期。

## 命令

脚本路径:`/srv/hermes/profiles/trader/skills/trading/v3-trader/scripts/v3_trade.py`(python3,无依赖)

```bash
V3=/srv/hermes/profiles/trader/skills/trading/v3-trader/scripts/v3_trade.py

# 开仓(市价做空,自动定量:带 --sl 即可,不传 --notional)
python3 $V3 open BTCUSDT short --sl 63000 --tp 60000,58500 \
  --reason "频道X信号: BTC空 入场CMP" \
  --account account-a \
  --channel -1002136478186 \
  --authorized-by-type channel \
  --authorized-by-id -1002136478186 \
  --source-message-id tg-sig-c1002136478186-m12345 \
  --ref tg-sig-c1002136478186-m12345

# 一条信号含多个入场价(首次入场+加仓/分批): 每个价位单独一笔 open,
# --ref 必须加稳定后缀区分(-e1 首入、-e2 加仓),否则第二笔会被幂等去重吞掉。
# 每笔独立自动定量(各约2%风险);若信号明示加仓量更小,给加仓单显式 --notional。
python3 $V3 open BTCUSDT short --entry-type limit --price 62663 --sl 64229 \
  --reason "频道信号首入" --account account-a \
  --channel -1002136478186 --authorized-by-type channel \
  --authorized-by-id -1002136478186 \
  --source-message-id tg-sig-c1002136478186-m4374 \
  --ref tg-sig-c1002136478186-m4374-e1
python3 $V3 open BTCUSDT short --entry-type limit --price 63457 --sl 64229 \
  --reason "频道信号加仓" --account account-a \
  --channel -1002136478186 --authorized-by-type channel \
  --authorized-by-id -1002136478186 \
  --source-message-id tg-sig-c1002136478186-m4374 \
  --ref tg-sig-c1002136478186-m4374-e2

# 限价/区间入场(同样自动定量)
python3 $V3 open ETHUSDT long --entry-type limit --price 2400 --sl 2320 \
  --reason "用户指令: ETH限价多" --account account-a --channel operator \
  --authorized-by-type user --authorized-by-id balen \
  --source-message-id operator-request-eth-2400 \
  --ref operator-eth-long-2400
python3 $V3 open SOLUSDT short --entry-type zone --price-min 145 \
  --price-max 148 --sl 152 --reason "用户指令: SOL区间空" \
  --account account-a --channel operator --authorized-by-type user \
  --authorized-by-id balen --source-message-id operator-request-sol-zone \
  --ref operator-sol-short-zone

# 信号没给止损 → 必须显式小额 --notional 并说明
python3 $V3 open BTCUSDT short --notional 300 \
  --reason "用户指令无SL,固定小额" --account account-a \
  --channel operator --authorized-by-type user --authorized-by-id balen \
  --source-message-id operator-request-btc-no-sl \
  --ref operator-btc-short-no-sl

# 审核发布 canary: 仅在 permit 已签发并 armed 后使用,名义金额不得超过 12 USDT。
# quantity 必须按实时价格和交易所步进预先计算;正常交易不传这三个 canary 参数。
# CLI 同时接受 --time-in-force/--time_in_force 与 --canary-permit-id/--canary_permit_id。
python3 $V3 open SOLUSDT long --entry-type limit --price 145 \
  --time-in-force IOC --quantity 0.08 --notional 11.6 \
  --canary-permit-id <permit-id> --reason "审核发布 account-a canary" \
  --account account-a --channel operator --authorized-by-type user \
  --authorized-by-id balen --source-message-id operator-canary-account-a \
  --ref operator-account-a-canary

# 整仓平掉某币种(市价 reduce-only)。管理动作一律要 --ref(本次操作的稳定幂等号,重试必须复用同一个);
# 处理频道消息时再带 --channel <本频道id> 与 --entry-ref <该仓入场时的 client_ref>(从 v3_query intent 审计链取)
python3 $V3 close BTCUSDT --side short \
  --reason "用户口头指令: 平掉BTC空" --account account-a \
  --authorized-by-type user --authorized-by-id balen \
  --source-message-id operator-request-close-btc \
  --ref close-btc-verbal-0714
python3 $V3 close BTCUSDT --side long \
  --reason "C02-舒琴消息3900: 平掉BTC多" --account account-a \
  --channel -1002136478186 \
  --entry-ref tg-sig-c1002136478186-m3856 \
  --authorized-by-type channel --authorized-by-id -1002136478186 \
  --source-message-id tg-sig-c1002136478186-m3900 \
  --ref close-btc-tg-sig-c1002136478186-m3900

# 部分平仓(按币的数量;--ref/--channel/--entry-ref 规则同上)
python3 $V3 partial SOLUSDT --side long --quantity 0.05 \
  --reason "信号: TP1到,减半" --account account-a \
  --channel -1002136478186 \
  --entry-ref tg-sig-c1002136478186-m3856 \
  --authorized-by-type channel --authorized-by-id -1002136478186 \
  --source-message-id tg-sig-c1002136478186-m3901 \
  --ref partial-sol-tg-sig-c1002136478186-m3901

# 调整已有仓位的止损(自动撤旧止损、按当前仓位数量重挂,reduce-only)
python3 $V3 set-sl BTCUSDT --side short --sl 62500 \
  --reason "用户指令: 止损上移到成本" --account account-a \
  --authorized-by-type user --authorized-by-id balen \
  --source-message-id operator-request-btc-sl \
  --ref set-sl-btc-operator-12346

# 管理动作必须加 --side 指明 long/short 仓位簿。
python3 $V3 set-sl ETHUSDT --sl 1725 --side long \
  --reason "用户指令: 多单止损调到1725" --account account-a \
  --authorized-by-type user --authorized-by-id balen \
  --source-message-id operator-request-eth-sl \
  --ref set-sl-eth-long-1725

# 替换已有仓位的全部止盈档(不给 --qty 时按当前仓位均分;会撤掉旧止盈)
python3 $V3 set-tps BTCUSDT --side short --tp 61500,60800,60000 \
  --reason "用户指令: 三档止盈" --account account-a \
  --authorized-by-type user --authorized-by-id balen \
  --source-message-id operator-request-btc-tps \
  --ref set-tps-btc-operator-12346

# 撤销单笔系统挂单(只能撤系统下的单,外部/手动单不可撤;订单号用完整35位)
python3 $V3 cancel WLDUSDT --order B<32位hex><2位序号> \
  --reason "48h超龄撤单" --account account-a \
  --authorized-by-type user --authorized-by-id balen \
  --source-message-id operator-request-cancel-wld \
  --ref ttl-<订单号后8位>
# cancel 的 --ref 每张订单必须独立(用订单号后缀),复用同一个 ref 会被幂等去重、第二张单撤不掉

# 查某笔订单执行状态 / 查全部持仓与余额
python3 $V3 status <intent_id>
python3 $V3 positions

# 账户: 每个写命令都显式传 --account account-a|account-b|account-c|account-d。
# 频道开仓时账号必须匹配 watcher 当前路由;管理动作由控制面按原开仓归属规范化。
```

## 输出解读

- 下单命令会自动等待约 30 秒,返回里 `execution_events` 的 `OrderFilled`(含 last_qty/last_px)= 成交;`rejected/denied` = 被拒;都没有 = 节点还没执行,稍后用 `status <intent_id>` 复查。
- 返回里的 `error` + `detail` 是服务端拒绝原因(超上限、缺参数、仓位不存在等)。
- 对冲模式:同一币种可以同时持有多空,开仓没有方向限制;平仓只平指定币种上现有的那个仓。

## 反馈规范(Telegram)——必须口语化

脚本输出的 JSON 是给你看的,**不是给用户看的**。给用户的消息必须是人话短消息,硬性要求:

- **禁止**粘贴 JSON、字段名(intent_id/execution_orders/replayed_existing_intent 之类)、UUID 全串、脚本原始输出。
- 2~4 行说清:做了什么 → 结果(成交价、数量)→ 一句依据。数字保留关键位数即可。
- 失败时用一句话说明原因(如"超单笔上限150U"),不贴错误堆栈。
- 需要留查询凭据时,只给 intent 前 8 位,如 `(单号 ce43edb5)`。

示例(成交):
> ✅ 已平掉 BTC 空单:0.002 BTC @ 61621.4,基本打平(-0.04U)。依据:你的口头指令。(单号 ce43edb5)

示例(开仓):
> ✅ BTC 市价开空 100U(0.0016 BTC @ 61600),止损 63000。依据:XX频道 17:15 信号。

示例(不执行):
> 🔕 XX频道这条是行情分析,无入场参数,不操作。

示例(失败):
> ⚠️ SOL 开多没成:超单笔上限 150U,已按 150U 重试成交 / 或说明卡在哪。

## 日报/周报发布

脚本路径:`/srv/hermes/profiles/trader/skills/trading/v3-trader/scripts/v3_report.py`(python3,无第三方依赖)

流程:

1. 先跑 `report_data.py` 拿数字摘要。
2. 用 `v3_query.py messages <频道> --limit <N>` 回看各频道当期消息,总结每个活跃频道交易员的行情观点。
3. 组装报告 JSON。`channel_views` 必填,每个活跃频道都要有 `{channel,trader,stance,summary,symbols}`；其中 `stance` 只能使用 `bearish`、`bullish`、`mixed`、`neutral` 四个英文枚举值之一。`sections.overview_md` 也必填(本期概览,3-6行);有拒单/裸仓/系统异常时 `sections.risk_md` 必填;周报建议再写 `market_md`(大盘走势与关键位)和 `actions_md`(下周计划)。
4. 有值得展示的盘面图,先 `python3 v3_report.py upload <图片路径>` 上传,再把返回的 asset URL 写进 `images[].url`。
5. 发布: `python3 v3_report.py publish --type daily --json report.json` 或 `--type weekly`。
6. 把 `v3_report.py` **返回的完整 URL 原样**发给用户（当前域是 `https://jp-bot.balen.wang/reports/...html`）。禁止改域名，尤其禁止改成 `hk.balen.wang`。

铁律:

- 禁止发 watcher 界面截图代替日报/周报。
- 禁止跳过频道观点总结;`channel_views` 是用户明确要求的报告核心。
- 脚本失败时只转述可读原因,不要把堆栈或原始 JSON 发给用户。
- 发给用户的报告链接必须是 publish 脚本打印的那一行，禁止按记忆或示例域名改写。
- `trade_outcomes` watermark 必须覆盖报告窗口尾。hourly timer 是主路径；若 publish 返回 503 且原因含 `window tail not materialized`，先 `systemctl start trader-v3-trade-outcomes.service`（幂等 upsert），再重试发布。禁止在物化滞后时把 KPI 全 0 的报告发出去。
