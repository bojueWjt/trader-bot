# quant-lab 回测看板

看板把同一信号的老师消息版本、解析结果、G1 图事件、K线和 L0 执行事件放在一起。仅供描述性研究；无交易、写数据或联网补行情接口。真实聊天与交易文件留在研究机，仓库只包含代码和合成夹具。

## 配置与启动

在研究机上创建 `dashboard.json`，按实际数据填写；不要把个人配置或真实数据提交到仓库。[config.example.json](config.example.json) 全部使用合成路径：

```json
{
  "reports": "/synthetic/research/reports",
  "tag": "v7",
  "market_lake": "/synthetic/research/lake/market",
  "channels": [
    {
      "key": "demo",
      "name": "合成老师",
      "channel_id": -1001234567890,
      "data_root": "/synthetic/channels/demo",
      "graph_version": "demo-v1"
    }
  ]
}
```

`tag` 缺省 `v7`。配置内相对路径基于配置文件目录解析。每个 `data_root` 对应 `Layout.from_root(root)`：`lake/telegram/bronze`、`silver`、`gold` 必须对应同一频道构建，包含源版本原文和解析结果。G1 真实构建 CLI 有时将 bronze/silver 留在 `_build/<scope>`；部署前确认所配置频道根确实包含相应版本，不能只提供 gold。`graph_version` 经 `gold/_manifest/_alias.json` 解析，并校验发布 manifest、文件哈希、tombstone 和过期 episode；与回测报告图版本不一致时拒绝详情。所有频道显式传 Layout，不切换进程环境变量。

研究机的 `quant-lab/` 下：

```bash
.venv-g2/bin/python -m quant_lab.viz \
  --config /synthetic/research/dashboard.json --port 8765
```

本机仓库 `quant-lab/` 下（服务配置已放在研究机）：

```bash
# 默认远端配置：/Volumes/G/quant-lab-data/dashboard.json
scripts/hackintosh.sh dash
# 或指定远端配置、本地端口、远端端口
scripts/hackintosh.sh dash /synthetic/research/dashboard.json 8876 8765
```

`dash` 沿用 `JUMP/HOST/SSH_OPTS` 和公钥认证；检查 `/api/health` 的服务身份及配置路径+内容摘要，已有匹配服务则复用。未运行时在研究机 `~/quant-lab` 用 `nohup` 以正常优先级启动（交互服务，批量回放占满 CPU 时仍要能打开），日志为数据根 `dashboard-<远端端口>.log`。就绪后以 `ssh -f -N -L 127.0.0.1:本地端口:127.0.0.1:远端端口` 打开隧道，打印访问 URL。代码部署需先通过现有 `sync` 流程；`dash` 不同步代码、不上传配置。

配置不符或端口被其他服务占用时启动失败；选择其他远端端口或手动停止明确识别的旧看板进程后重试。本地端口已占用会由 `ExitOnForwardFailure` 拒绝。隧道在后台持续运行；用本机进程列表找到对应 `ssh -f -N -L ...` 后停止该进程即可关闭。研究机服务用进程列表按 `quant_lab.viz --config ... --port ...` 精确识别后停止。修改配置或服务代码后需重启看板。

默认仅绑定 `127.0.0.1`。显式 `--bind 0.0.0.0` 等非 loopback 地址会打印警告；服务没有认证，正常使用 SSH 隧道。

## 页面与统计

- `/`：频道 × 口径统计与每频道多口径累计 R 曲线。
- `/channel?channel=demo`：跨口径信号列表。成交状态、出场方式、老师执行数与 R 排序按选择的口径；筛选成交、老师指令、币种和 UTC 月份。
- `/trade?channel=demo&variant=base&episode=<已发现ID>`：同一信号口径切换、K线与标记、分段止损、消息／执行事件混排和成本／MAE／MFE 卡片。点事件定位 K线，点 K线定位最近事件。

口径目录为 `<reports>/l0-<tag><suffix>/<channel_key>/`，必须同时有可读取且相容的 `trades.parquet` 与 `summary.json`。缺失、损坏、写入期间变化、parquet 新于 summary、频道／图／策略哈希不符或笔数不符都显示「未出」。每次请求重新发现口径，不要求重启。内置缺口径也显示「未出」；其他已发现后缀原样显示。

| suffix | 名称 |
| --- | --- |
| 空 | 5天（API key `base`） |
| -be1 | 保本 |
| -w14 | 14天 |
| -w14-be1 | 14天+保本 |
| -live | 让点 |
| -be1-live | 让点+保本 |
| -follow | 跟指令 |
| -live-follow | 让点+跟指令 |
| -w60 | 60天 |
| -w60-be1 | 60天+保本 |

### v8 口径（`"tag": "v8"`）

`tag` 为 `v8` 时不显示上表的 v7 内置口径，改用 runbook §8 第 7 步的目录：

| 目录 | API key | 名称 |
| --- | --- | --- |
| l0-v8-w60lf-ns300 | w60lf-ns300 | 主口径（60天·让点·跟指令·无止损300U），频道页默认 |
| l0-v8-w1-ns300 / -5d-ns300 / -w14-ns300 | w1-ns300 / 5d-ns300 / w14-ns300 | 无止损持有期档 |
| l0-v8e / l0-v8w / l0-v8nw | v8e / v8w / v8nw | 变体图：编辑敏感性 / 宽口径 / 不等待止损 |

其他 `l0-v8-*` 目录（S 批次等）照常按后缀发现。C 批次（与 v7 对照，B=100）按 runbook 命名为 `l0-v8cmp*`，`l0-v8` 之后没有连字符，按 `cmp` 前缀单独发现（口径名 `v8cmp…`，用主图）；除此之外不带连字符的目录只认上表三个，不把任意 `l0-v8xxx` 当口径。

变体图（`<ch>-v8e` 等）与主图在同一个数据根。详情页按口径核对报告的图版本：主口径、持有期档、S/C 批次只接受频道配置的 `graph_version`；`v8e`/`v8w`/`v8nw` 只接受 `graph_aliases` 里属于它自己的图——写成 `{"v8e": "<ch>-v8e", "v8w": "<ch>-v8w", "v8nw": "<ch>-v8nw"}`，或列表 `["<ch>-v8e", "<ch>-v8w", "<ch>-v8nw"]`（按 `-v8e` 等后缀归属）。没列出的、或变体图报告放进了主口径目录的，都按「配置图版本与回测报告不一致」拒绝。

**分块**（方案 F1、§11 第 7 条）：v8 summary 的 `overall` 只统计 `sizing_basis=risk` 的行，无止损行在 `blocks.nostop`；看板判断 parquet 与 summary 是否成对时按 `overall.n_trades + blocks.nostop.n_trades` 核对笔数。首页的均值 R、胜率、95% 区间和累计 R 曲线只用按风险定量（有止损）的单；无止损单（固定名义，仅内核 A）单独按金额 U 统计（n、均值 U、胜率、合计 U、按日聚类区间）；两块的合计只给金额 `sum_net_U = Σ net_R × risk_budget`，不出合计均值和胜率。任一可评估行缺 `risk_budget` 时金额为空（不当成 0）。旧报告没有 `sizing_basis` 列，全部视为按风险定量，统计输出与 v8 之前逐字段相同。频道列表里无止损单的结果列按 U 显示，另有「定量」（按风险 / 无止损名义 / >3腿）与「合并」（`plan_link_kind`、被并入本单的 dup 数）两列；dup 数按所选口径报告自己的图版本计（v8e/v8w/v8nw 的合并与主图不同），图里没有 `dup_of` 列时不显示；每个口径的记录只带该报告真有的 v8 列，旧报告不带空字段；详情卡片显示 `sizing_basis`、`net_U`、`mae_U`、成交名义、删失时浮动盈亏、G1 的合并方式、重发家族、并入本单的消息（`dup_of` 指向本单的 episode）、止损规则、无止损类别、场所、分诊、升级范围、信号年龄、编辑延迟和第二遍判定。图里没有这些列（B 组合入前）时不显示。

长期是否盈利只看 `scripts/v8_report.py` 的冻结判定（方案 §10.4：周块 bootstrap、判定期止于 2026-06-30、四档持有期一致），看板的按日区间只作浏览。

可评估样本同时要求：`fill_status ∈ {filled, partial}`、`censor_reason` 空、`net_R` 非空，且 `mark_ok/funding_ok/rules_ok/bars_ok` 严格为 true。胜率为 `net_R>0` 的比例；点估计为逐笔均值，合计 R 为逐笔累加。95% 区间以 UTC `t_dec` 日期分组的天均值计算：天均值的均值 ± `1.96 × sample_std / sqrt(n_days)`。少于两天区间为 null、显示不足；下沿 >0 正期望，上沿 <0 负期望，否则 ≈0。≈0 也包含证据不足的情况。

**与旧 summary 的差异**：当前 `l0_replay.summarize()` 的 `overall.mean_net_R` 与累计曲线含未成交的零 R；看板按照本需求的可评估“已成交单”统计，对应 summary 的 `n_evaluable_filled` 和成交样本胜率。看板不修改 summary、不沿用包含未成交单的旧均值／曲线，也不改变 L0 输出。

## 单笔复算与事件线

共享 `l0_replay.prepare_episode_request()` 按原顺序执行参考价补全／过时报价限价化、无止损近端单笔与固定名义定量、live profile、历史 tick 规则、`build_request`，使用 summary 中的策略版本和风险预算。与回放相同，episode 带 `stop_source_version_id` 时另读该止损消息的原文作为 `stop_text` 传入（live v4 按止损所在消息判 0.1% 放宽），而不是只传根原文。follow 口径再读取 summary 的 `follow_teacher.path`，调用原 `attach_management`。随后原 `load_market_from_lake` + `simulate_batch(kernel="A")` 得到 canonical events。策略哈希与登记不一致、根不可执行或输入改变时拒绝复算。

只有复算 `trace_hash` 与该口径交易行完全一致才返回和展示成交事件；例外是内核版本不同（如 v0.5 批量结果、v0.6 复算）而成交、价格、时间、费用、R、覆盖标记等全部结果字段逐一相同，此时标「结果一致（内核版本不同）」并展示事件。否则显示「复算不一致」，保留回测行的数据卡片与原文，隐藏 canonical events；不将当前新模拟伪装成历史成交。指令“采用”是请求构造结果，“执行结果”来自 canonical management 事件；采用但未处理可能是仓位已结束。不跟指令的口径标 `policy_disabled`。`uncertain`、`episode_ambiguity`、超窗和其他丢弃原因单独展示；归属未确定但窗口内提及同币种的指令作为上下文展示，不因此采用。

消息按精确 source version 展示版本号、reply_to、发布时间、available_at、解析结果和关联图事件；默认以可用时间排序，缺失时回退事件／发布时间，完全未知的置末。同一时刻消息先于模拟事件，模拟事件按 seq 排序。窗口内同币种消息由解析品种与正文代号／已有中文别名匹配；正文匹配只用于查看上下文。

K线范围为 `t_dec - 1天` 至 `horizon_end + 1天`。服务端读取 lake 的 `klines/markPriceKlines` 1m silver 分区并聚合为 5m/15m/1h/4h；默认选择最细且不超过约 1600 根的周期。缺失分钟不补齐，不完整聚合返回 `partial=true`。执行仍用原体检和覆盖规则的 1m loader，图形聚合不参与回测。图上标记落在所属或最近可用 K线，事件线保留精确模拟时刻、价格、数量与占累计入场成交量的比例。

详情复算缓存仅写 `<reports>/_dash_cache/`。文件名为 `(口径, 频道, episode_id, 历史 trace_hash)` 的 SHA-256；JSON 内保存该身份、输入指纹和模拟结果。每次读取检查源图、消息、解析、follow 文件、本单窗口（t_dec 前 2 天至观察窗后 1 天）内该品种的 1m/资金费日分区与月 manifest、品种规则和代码的路径+mtime_ns+大小指纹；变更即重新模拟。不遍历整个行情湖（外接盘上逐文件 stat 会让一次详情等十几分钟）。缓存不保存消息原文。缓存写入原子替换；reports 不可写时退回无缓存计算。禁止通过客户端指定缓存路径。服务内串行化数据请求，防止并发重复复算和过量占用研究机 CPU。

## GET 接口

| 路径 | 参数 | 返回 |
| --- | --- | --- |
| `/api/health` | 无 | service、config_id |
| `/api/overview` | 无 | tag、variants、channels.statistics |
| `/api/channel` | channel | 口径状态、跨口径交易、teacher_episode_ids |
| `/api/detail` | channel、variant、episode；可选 interval | 原始 trade、执行 plan、consistency、events、timeline、bars、stop_segments |

Decimal 返回十进制字符串，统计量返回数值，时间为 ISO UTC；图形 bar time 为 Unix 秒。未知频道／口径／episode、重复／未知参数、路径穿越、非法 interval 返回 404；源数据或复算身份冲突返回 409；其他读取异常返回 500 并记本地日志。修改类方法和 HEAD/OPTIONS 返回 405。不接收路径参数、无 CORS；所有数据仅通过 `textContent` 创建节点，CSP 限制脚本来源。

## 验证与限制

```bash
nice -n 19 .venv-g2/bin/python -m pytest tests/viz -q
nice -n 19 .venv-g2/bin/python -m pytest tests/market -q
nice -n 19 .venv-g1/bin/python -m pytest tests/data -q
bash -n scripts/hackintosh.sh
```

合成测试贯通发布图 → L0 → 单笔重算，覆盖基线／live／follow、trace 一致与不一致、聚类统计、缺／在跑口径、原文 HTML、未采用原因、排序、缓存失效、GET 路由与拒绝写入。HTTP 测试在允许监听的环境启动临时端口；若沙箱拒绝 bind，使用相同 BaseHTTPRequestHandler 的内存 HTTP 请求，仍检查 JSON、状态码和页面资源。静态检查元素 id 与脚本引用，并用 `node --check` 检查 JavaScript。

原有断言仅更新 `tests/data/test_smoke.py::test_api_signatures_match_contract` 的两处签名期望：`load_episodes` 参数列表增加 keyword-only `layout=None`，`load_episode_events` 同样增加 `layout=None`；其他断言不变。这是本需求允许的纯调用签名更新，不改变数据／回测预期。

允许监听时可在合成配置上启动后执行：

```bash
curl -fsS http://127.0.0.1:8765/api/health
curl -fsS http://127.0.0.1:8765/api/overview
curl -fsS 'http://127.0.0.1:8765/api/channel?channel=demo'
curl -fsS 'http://127.0.0.1:8765/api/detail?channel=demo&variant=base&episode=<合成ID>'
```

Lightweight Charts 固定 **4.2.3**，浏览器需要能访问 unpkg.com；只从 CDN 下载脚本，不向 CDN 发送聊天／交易内容（`Referrer-Policy: no-referrer`）。CDN 不可用时显示加载失败，表格、消息和数据卡片仍可查看。当前未做分页，频道很大时读取与首屏较慢；60 天首次复算可能耗时，后续缓存复用。缓存指纹基于文件元数据，不防有人故意保持相同 mtime 与大小篡改文件；它服务于本地可信研究机的数据更新检测。没有实际研究机部署、SSH 隧道或真实数据验收时，应在交付中明确列出，不能将合成测试当作线上完成。
