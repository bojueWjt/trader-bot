# wac-063 第①步 平板真机布局验收（无后端）

**结论：FAIL。2 个 🔴，8 个 🟡，4 个 🟢。**

首要阻断有两个：
1. 横屏弹出键盘时，获得焦点的输入框被遮挡，页面既不避让也不自动滚动。
2. 侧栏宽度固定约 360dp，导致中等档（约 680dp）的内容区只剩约 320dp；竖屏宽屏档下信号页右栏只剩约 148dp。

## 环境

- 设备：adb 0486209049983540，M367FC，Android 17（SDK 37），`wm size` 2272x3408，`wm density` 400，开始和结束各测一次，一致。
- 被测：alert-personal 73a3e39，构建于 `.worktrees/wac-063`（临时未提交改动 3 行：applicationId 加 `.wac`、测试应用名、深链 scheme 改成 `attention-wac063-test`）。
  - release 构建，JS 打包进 APK，签名证书 CN=Android Debug。
  - 设备上 base.apk 的 sha256 与构建产物一致：b5954097…af6。
- 安装：测试包由 Planner 在用户点"继续安装"后装好（22:58:40），本步未重装。
- trader-bot 集成分支：452077b。
- 时间：2026-09-29 22:59–23:16（设备时钟）。
- 证据目录：`/private/tmp/claude-501/-Users-balen-projects-trader-bot/e75fc499-c012-4132-9bf5-78b4f2f83148/scratchpad/wac063/`
  - 下文截图名省略 `.png`。
  - 每张截图的前台窗口与 Configuration（swdp/wdp/hdp/dpi/窗口边界/窗口模式）都记在 `shots.log`。

## dp 实测（来自截图时 dumpsys 里测试包 Activity 的 CurrentConfiguration）

| 窗口形态 | 取得方式 | 实测 | 档位（600/840） |
|---|---|---|---|
| 竖屏 | 旋转锁 user_rotation=0 | w909dp h1363dp | expanded |
| 横屏 | user_rotation=1 | w1363dp h909dp | expanded |
| 横屏半屏 | freeform 窗口，`am task resize 0 82 1700 2272` | w680dp h876dp | medium |
| 横屏 1/3 | freeform 窗口，`am task resize 0 82 1136 2272` | w454dp h876dp | compact |
| freeform 默认 | `am start --windowingMode 5` | w412dp h732dp | compact |
| 显示大小放大一级·竖屏 | `wm density 440` | w826dp h1239dp | medium |
| 显示大小放大一级·横屏 | `wm density 440` | w1239dp h826dp | expanded |

说明：
- 半屏和 1/3 屏用 freeform 窗口加 `am task resize` 精确控制宽度，不是 HyperOS 的系统分屏手势。系统分屏需要另一侧放一个应用，并在用户界面上拖动，本步没做。对布局来说，两者给 app 的只是窗口宽度，结论等价，但标为"近似"。
- "放大一级"用 `wm density 440` 代替（计划 §7A.2 表格里的 440 档），不是在系统设置里点"显示大小"。测完已 `wm density reset`。

像素测量（400dpi 下 2.5 px = 1dp）：
- 侧栏右缘在 x=899/900px，即 **360dp**。竖屏、横屏、半屏同宽；半屏 freeform 实测 917px，约 367dp，含窗口边框。
- 信号页竖屏：左栏 900–1898px，即 **399dp**；右栏 1901–2272px，即 **148dp**。
- 单列页（服务连接、设置、配对、Trading 配置）在竖屏和横屏下居中，输入框宽 680dp，加两侧 20dp 内边距等于 **720dp**，符合 maxWidth 720。

## 逐项结论

### 1. 断点与导航

| 检查 | 期望（计划 §7A.1） | 实测 | 结果 | 证据 |
|---|---|---|---|---|
| 竖屏 909dp | expanded：左侧栏带标签；单列页居中 720；信号双栏 | 侧栏 ✓，单列 720 ✓；信号双栏只有 399 + **148dp** | **FAIL（🔴-2）** | p02-home-portrait、p03-trading-unconfigured-portrait、p04-signals-unconfigured-portrait、p05-settings-portrait |
| 横屏 1363dp | expanded：侧栏；单列 720；信号 400 + 右栏（上限 840） | 全部符合，右栏 603dp | PASS | l12-home-landscape、l13-trading-setup-landscape、l16-settings-landscape、l17-signals-unconfigured-landscape、l29-signals-unreachable-landscape、l34-pairing-landscape |
| 半屏 680dp | medium：侧栏；信号单栏，push 详情；返回键 | 侧栏 ✓，信号单栏 ✓；切到"简报"后返回键回到"消息" ✓；但内容区只剩约 **313–320dp** | **FAIL（🔴-2）** | f01-freeform-half-home、f02-half-signals、f03-half-settings、f07-half-trading、f08-half-signals-briefings、f09-half-signals-after-back |
| 1/3 屏 454dp、freeform 412dp | compact：底部 Tab | 底部 Tab ✓；切档后仍停在"信号"Tab ✓ | PASS | f10-third-signals、f11-third-trading、f12-third-settings、f13-third-home、f00-freeform-default |
| 显示大小 440 | 竖屏 826 → medium；横屏 1239 → expanded | 竖屏侧栏加信号单栏 ✓；横屏信号双栏 ✓；侧栏仍约 360dp | PASS（同 🔴-2 的侧栏问题） | d01-density440-portrait-home … d04-density440-landscape-signals |
| 旋转保持状态 | 不重挂载、保留 Stack 与 Tab | 在持仓页从横屏转竖屏仍停在持仓页；返回后回到交易 Tab，滚动位置保留 | PASS | l32-positions-error-landscape、p06-positions-error-portrait-after-rotate、p07-back-to-accounts-portrait |
| 抽屉 BottomSheet（最大宽 640） | — | 只挂在仓位详情页，需要仓位数据，本步到不了 | **未覆盖** → 第②步 | — |
| 账户卡列数、仓位双栏 | A-T 范围 | 现状单列通栏，持仓页全屏 push（看板 A-T 仍 pending） | 仅记录，不判 | l28-accounts-error-landscape、l32-positions-error-landscape |

### 2. 旋转与横屏软键盘：FAIL（🔴-1）

键盘（IME）顶边位置，均为横屏、3408x2272 坐标：

| 键盘 | IME 顶边 y |
|---|---|
| 用户默认输入法 Typeless（语音键盘，较矮） | 1522px |
| 小米键盘，普通字段 | 1445px |
| 小米键盘，密码字段 | 1167px |

键盘弹出时，测试包窗口的 frame 仍是 `[0,0][3408,2272]`，窗口没有缩小（dumpsys window）。

- **服务连接页（Setup）**
  - 用 Typeless 聚焦 App ID 后，上划内容区不滚动（l05-setup-landscape-kbd-appid-swipe）。"保存、注册设备并连接"按钮（y 1523–1641px）被键盘盖住，键盘开着时够不到（l03-setup-landscape-kbd-url）。
  - 换小米键盘后，"安全 token"输入框只露出上半截（l06-setup-landscape-mikbd-appid）；上划依然不滚动，手势只会收起键盘（l07-setup-landscape-mikbd-swipe）。
  - 密码框聚焦时，小米键盘顶边在 1167px，token 框在 1367–1486px，**完全被遮**。这一状态截不到图：输入法窗口带 FLAG_SECURE，系统把整屏截成黑色（l02-setup-landscape-kbd-token、l08-setup-landscape-mikbd-token 为全黑，均值 0.4）。这里用 dumpsys 的 IME inset 坐标作证据。
- **设置 → Trading API（控制面地址）**
  - 聚焦后输入框在 1688px，键盘顶边 1522px，**输入框完全不可见**；输入字符后页面也不自动滚动（l19-settings-landscape-kbd-url、l20-settings-landscape-kbd-typed）。
  - 只能手动上划把它拖出来（l21-settings-landscape-kbd-swiped）。这一页下方还有内容才拖得动；表单如果在页尾就拖不出来。
- **交易 Tab 的 Trading 配置表单**：表单在页面上部，Typeless 下没有被遮（l14-trading-setup-kbd-url）。这只是恰好没遮，不是做了避让。
- 计划 §7A.2 原文是"横屏打开键盘时输入框不被遮挡"，以上情况不满足。

### 3. 无后端时的空态、错误态与断线提示

| 页面 | 状态 | 实测 | 结果 | 证据 |
|---|---|---|---|---|
| 服务连接保存 | 假地址 `https://wac-test.invalid` | 弹窗"连接失败 / Network request failed"，页面内也有红字 | PASS（🟢-1 文案） | l11-setup-save-failed-alert |
| 告警 | 已配置、未注册 | "在线 · 待确认 0 · 恢复队列 0"，空态"当前没有待处理请求" | PASS | l12-home-landscape |
| 交易（未配置） | — | 显示"配置 Trading API"表单，居中 720 | PASS | l13-trading-setup-landscape、p03-trading-unconfigured-portrait |
| 交易（`http://127.0.0.1:1` + 随机 token） | 请求失败 | 红字"Network request failed"；权益图显示"Network request failed / 重试"；四张卡都是"占位"和 +$0.00；**但新鲜度条是绿色 "fresh · projection_lag_ms=0"** | 不崩溃、不白屏；断线提示有误导（🟡-1） | l28-accounts-error-landscape、l31-accounts-bottom-landscape、f11-third-trading |
| 持仓 | 请求失败 | "持仓加载失败 / Network request failed / 重试"，居中 | PASS | l32-positions-error-landscape、p06-positions-error-portrait-after-rotate |
| 信号（未配置） | R16 四态 | 状态条"信号 · 接口失败"，副标题与列表都显示"交易服务未配置"，右栏"选择一条记录查看详情" | PASS（文案）；布局问题见 🔴-2、🟡-3、🟡-4 | p04-signals-unconfigured-portrait、l17-signals-unconfigured-landscape |
| 信号（已配置，不可达） | R16 四态 | "信号 · 接口失败 / 采集服务不可达" | PASS | l29-signals-unreachable-landscape、f02-half-signals、f10-third-signals |
| 设置 → 采集服务 | 不可达 | 400dpi 下重启后显示"交易服务未配置"或错误信息；440dpi 重启后显示"网络请求失败"，与信号页的"采集服务不可达"不一致 | PASS（🟢-2） | l30-settings-watcher-unreachable-landscape、d03-density440-portrait-settings |

- 没有出现白屏或红屏。
- R16 四态里，无后端只能触发"接口失败"这一态；"正常 / 静默 / 失活"三态需要后端，转第②步。
- 本步未点"断开"或"重连"。

### 4. logcat 与 gfxinfo 基线

**logcat**：22:59:45 起全程抓取（`logcat-full.txt`，67k+ 行），按测试包 6 个进程号过滤得 `logcat-testpkg.txt`（5171 行）。
- **没有 FATAL、没有 AndroidRuntime 崩溃、没有 ANR，也没有 ReactNativeJS 级别的错误或警告**；ReactNativeJS 只有 8 条 `Running "AttentionAndroid"`。
- E 级只有 ROM 噪声（LB、MI-PreRender、FrameInsert、Zygote、HWUI pipeline cache），以及切换显示大小和旋转时 RN surface 重建产生的 SurfaceMountingManager "stopped surface" 日志。
- 6 个进程全部由我的 `am force-stop` 结束（logcat 有对应的 adbd 与 ActivityManager 记录），没有异常死亡。
- 日志里的 "Legacy Architecture / WebSocketService" 警告来自 pid 11694，是 Typeless 输入法，不是测试包。

**gfxinfo 基线**：横屏设置页，reset 后上下各滑 5 次（`gfxinfo-settings-scroll.txt`）。

| 指标 | 数值 |
|---|---|
| Total frames | 2396 |
| Janky frames | 0（0.00%）；legacy 口径 2（0.08%） |
| 帧耗时 p50 / p90 / p95 / p99 | 5 / 5 / 5 / 6 ms |
| GPU p99 | 4 ms |
| Missed Vsync | 0 |
| Slow UI thread | 0 |

"High input latency 2396" 是 adb 注入输入造成的，不计。

**meminfo 基线**（同时刻，`meminfo-baseline.txt`）：TOTAL PSS 317 MB，其中 Graphics 199 MB、Native Heap 44 MB、Java Heap 14 MB；TOTAL RSS 504 MB。见 🟡-6。

### 计划具名用例（单元侧，非真机）

T7A-1/2/3/5 与 T7A-6 的信号页部分，对应套件 layoutClass、tabletNavigation、tabletSourceGuard、contentWidth、bottomSheet、rootTabs、signalsScreen：**7 个套件 48 个用例，连跑 3 次全过，没有靠重试**（`jest-t7a.txt`）。T7A-4 仓位双栏属于 A-T，尚未实现。

## 问题清单

**🔴-1 横屏键盘遮挡输入框，且窗口不随键盘缩小**
- 现象：见第 2 节。
- 根因（有证据链）：
  - targetSdk 36 在 Android 17 上强制 edge-to-edge，`windowSoftInputMode=adjustResize` 不再缩小窗口：IME 出现时窗口 frame 仍是全屏，IME inset 为 `[0,1167..1522][3408,2272]`。
  - SetupScreen 的 `KeyboardAvoidingView behavior` 在 Android 上是 `undefined`。
  - SettingsScreen 和 TradingScreen 的 ScrollView 外层没有任何键盘避让，也不在聚焦时滚到输入框。
- 计划依据：§7A.2 与 §7A.1 抽屉条的"横屏时底部安全区与键盘避让重新验收"。

**🔴-2 侧栏固定约 360dp，窄窗口内容被挤压**
- 现象：
  - medium（680dp 半屏）内容区只剩约 313–320dp，比 compact 的 454dp 窗口还窄。
  - expanded 竖屏（909dp）内容区 549dp，信号页仍按双栏排（左 400 + 右 148dp），右栏无法显示正文和大图。
- 根因：
  - `RootTabs.tsx` 只设了 `tabBarPosition:'left'` 和 `tabBarLabelPosition:'beside-icon'`，没有限制侧栏宽度，实测每档都是 360dp。
  - `SignalsScreen.tsx` 用窗口档位 `expanded` 决定双栏，没有减去侧栏占用的宽度。
- 计划依据：§7A.1 要求"medium 与 expanded 用侧栏"、信号页"expanded：左栏消息流，右栏消息详情"。当前实现在计划实测的"平板竖屏 909dp"和"横屏半屏 680dp"两种目标窗口下不可用。

**🟡**
- **🟡-1 账户页请求失败时新鲜度条仍是绿色 "fresh · projection_lag_ms=0"，四张卡显示 +$0.00 占位值**（l28-accounts-error-landscape）。运维人员可能把零值误读为最新数据。AccountsScreen 从 0f7d26d 起没改过，属于存量问题、非本轮回归；但它直接落在"断线提示"检查项里，建议单独立项，优先级高。
- **🟡-2 设置页保存 Trading 配置没有成功反馈，采集服务状态行也不重新读取**。保存后仍显示"交易服务未配置"，交易 Tab 已挂载的表单也不会刷新，要重启才生效（l26-settings-after-save2、l27-trading-after-settings-save-stale）。
- **🟡-3 信号页未配置或不可达时，"断开""重连"仍显示为可点击的绿色链接**（api=false 时点了不起作用）。应置灰或隐藏。本步未点击。
- **🟡-4 信号页列表的空态和错误文字没有内边距**，贴着栏左缘（c-p04-top、c-f02）。
- **🟡-5 侧栏选中项是绿色文字配蓝色底，对比度 1.82:1**：底色 rgb(52,120,246)，字色 rgb(94,194,105)，低于 WCAG AA 的 4.5:1。原因是 NavigationContainer 没传主题，侧栏选中底色用了 React Navigation 默认主题的 primary。
- **🟡-6 内存基线**：没有任何数据时 TOTAL PSS 已有 317 MB（Graphics 199 MB，与 3408x2272 的缓冲有关），已超过 §5.2 "内存峰值 < 300 MB" 的门槛。第②步前需要 Planner 明确平板上的口径（PSS 还是 Java/Native 堆、以什么为基线），否则这一条必然 FAIL，而且说明不了媒体内存问题。
- **🟡-7 签名**：正式包 `com.pudutech.attention` 的证书短哈希是 51ed3f60，与本测试包（仓库 `debug.keystore`，CN=Android Debug）完全相同，说明正式包也是用仓库公开的 debug key 签的。
  - 更正上一版报告："签名不同、不可能误覆盖"这个说法是错的。实际防止误覆盖的只有包名不同。
  - 影响：凡拿到仓库 debug.keystore 的人，都能通过 USB 给正式包装升级（仍需设备上点确认）。
  - 建议 Release Steward 评估是否换正式签名。
- **🟡-8 操作侧副作用（我造成的）**：我在 23:00:10 跑过一次 `uiautomator dump`，它临时接管无障碍，导致开关控制（Switch Access）服务重新绑定并把设置向导拉到前台；同时小米系统语音引擎（com.xiaomi.mibrain.speech）弹出隐私同意页 CTAActivity（x01-foreign-cta，logcat 23:00:11）。
  - 两者我都没有点击，现在都在后台：开关控制向导 task #306，语音引擎同意页 task #305。
  - 开关控制向导在测试开始前就在前台，不是我首次拉起的。
  - 之后改为只用截图测量，不再使用 uiautomator。

**🟢**
- 🟢-1 错误文案直接透出英文 "Network request failed"（服务连接、账户、持仓）。
- 🟢-2 同一不可达情形，设置页显示"网络请求失败 / 交易服务未配置"，信号页显示"采集服务不可达"，口径不一致。
- 🟢-3 release 包的设置页底部仍有 "Ant Design prototype (dev)" 入口。
- 🟢-4 Home 列表、账户卡在 expanded 下都是通栏（1003dp）。计划没有要求它们限宽，A-T 会处理账户卡，这里只记录。

## 给 App Executor 的修复点

1. **键盘避让（🔴-1）**
   - Android 上显式处理 IME inset：SetupScreen 的 KeyboardAvoidingView 在 Android 用 `behavior="height"` 或 `"padding"`；SettingsScreen、TradingScreen 的 ScrollView 外层加同样处理，或统一封装成一个 `KeyboardAwareScroll`。
   - 聚焦时把输入框滚进可视区（`scrollResponderScrollNativeHandleToKeyboard` 或 onFocus 后 `scrollTo`）。
   - 横屏验收要同时覆盖密码字段和页尾表单。
   - 补一个 jest 用例：模拟 `keyboardDidShow`，断言内容 paddingBottom 或 ScrollView 偏移发生变化。
2. **侧栏宽度（🔴-2）**
   - 在 `RootTabs.tsx` 的 `tabBarStyle` 里给侧栏明确宽度。建议 medium 用只显示图标的窄栏（约 80dp，标签放图标下方），expanded 用约 200–240dp 带标签。
   - SignalsScreen 的双栏判断改为按内容区可用宽度（onLayout 或窗口宽度减侧栏宽度，要求 ≥ 400 + 最小右栏宽，例如 440dp）。不满足时按单栏加 push 处理。
   - 补 T7A-3 或 T7A-6 用例：909dp 和 680dp 两个宽度下，侧栏宽度与信号栏数的断言。
3. 🟡-3 信号页在 api=false 时，断开和重连按钮置灰。🟡-4 列表空态加 `padding: spacing.lg`。
4. 🟡-5 给 NavigationContainer 传入暗色主题，或设置 `tabBarActiveBackgroundColor`，让选中态对比度达到 4.5:1 以上。
5. 🟡-2 设置页保存后给出成功提示，并调用 `load()` 刷新采集服务状态。交易 Tab 在获得焦点时重新读取配置。
6. 🟡-1 需要 Planner 决定是否立项：请求失败时新鲜度条应显示失败或未知，卡片不应显示 0 值。

## 未覆盖项与原因

- BottomSheet 抽屉（最大宽、横屏键盘、返回键）、仓位双栏、止盈止损与平仓输入框：需要仓位数据，转第②步。按用户约束，第②步也不会点提交。
- R16 的"正常 / 静默 / 失活"三态、信号双栏选中与详情、媒体、滚动性能与内存峰值：需要本地测试后端，转第②步（wac-100）。
- 手机档与改前版本逐屏对照：本步只在平板 compact 窗口（454dp、412dp）确认底部 Tab、卡片和表单正常；没有 0f7d26d 基线构建的对照截图，也没有用手机实机，转第②步或由 Planner 另派。
- Pixel Tablet 模拟器同组截图（§7A.2）：未做。
- 系统原生分屏手势与系统"显示大小"设置：分别用 freeform 加 `am task resize`、`wm density 440` 近似代替，原因见"dp 实测"一节的说明。

## 设备与正式包状态

**正式包 `com.pudutech.attention` 前后一致（diff 结果为 IDENTICAL）**：
- versionCode=1
- lastUpdateTime=2026-09-22 23:15:55
- signatures=PackageSignatures{9fa9ebe version:2, signatures:[51ed3f60]}

共核对四次：22:40、22:59、23:05（暂停时）、23:16（`official-*.txt`）。对正式包只执行过 `dumpsys package`。logcat 中与它相关的只有这几条 dumpsys 记录，以及 density 变化后桌面重新加载应用列表的记录。

**设备设置（均已恢复，diff 结果为 SETTINGS_RESTORED）**：

| 设置 | 改动 | 恢复为 |
|---|---|---|
| system accelerometer_rotation | 测试中置 0 | 1 |
| system user_rotation | 测试中 0/1 切换 | 0 |
| wm density | 临时设为 440 | `wm density reset`，Physical 400，无 override |
| 默认输入法 | 临时切到 com.xiaomi.type 约 1 分钟 | com.typeless.mobile/.keyboard.KeyboardRNService |

以下项未改动，前后一致：font_scale 1.0、开发者选项 1、三项动画缩放 1、无障碍（enabled_accessibility_services 仍为 SwitchAccessService，accessibility_enabled=1）、screen_off_timeout、stay_on_while_plugged_in。

**测试包 `com.pudutech.attention.wac`**：
- 按 Planner 要求未卸载，停在"告警"首页、竖屏、全屏模式（task #313）。
- 包内只有假值：Attention 服务 `https://wac-test.invalid`，tenant/app 为 `wac063-*`；Trading 为 `http://127.0.0.1:1`；两个 token 都是随机串 `wac063…`。
- 没有输入过任何真实凭据，也没有连接任何生产域名。
- 第②步接入真实后端时，需要用户在该包里覆盖这些配置。

**未点击**：任何交易、平仓、撤单、HALT、RESUME、断开或重连按钮，以及开关控制向导、语音引擎同意页。点过的按钮只有：测试包内两次假值配置的保存、"飞书与设备配对"（只进入页面，未生成密钥）、"查看持仓"、各个 Tab。
