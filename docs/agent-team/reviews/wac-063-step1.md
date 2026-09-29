# wac-063 第①步 平板真机布局验收：BLOCKED（设备非空闲，未上机）

- 时间：2026-09-29 22:40–22:45（设备时钟）
- 设备：adb 0486209049983540，M367FC，Android 17（SDK 37），`wm size` 2272x3408，`wm density` 400（开始与结束各测一次，一致）
- 被测源码：alert-personal `integ/watcher-app-crew` 73a3e39，构建 worktree `.worktrees/wac-063`（detached 73a3e39，临时未提交改动见下）
- trader-bot 集成分支：452077b

## 结论

**BLOCKED，未执行任何上机用例。** 首要阻断：安装测试包时设备正在被他人操作。

1. `adb install`（新包，无 -r）返回 `INSTALL_FAILED_USER_RESTRICTED: Install canceled by user`。
2. 随即截图（`00-install-prompt.png`）：设备前台是"设置 → 搜索 Usb → USB 安装管理"，弹出的 MIUI "USB 安装提示"对应的是 **pad-read**（`work.padread.app`），不是本测试包。
3. `dumpsys package work.padread.app`：`firstInstallTime=2026-09-29 22:43:33`，即在我的安装请求之后一分钟内被装上。本机 `ps` 未见其它 adb 客户端，说明是用户本人或另一台主机或会话在同时往平板装应用、改 USB 安装设置。
4. 第二张截图（`00b-focus-now.png`）：设备停在"USB 安装管理"页，pad-read 在"已禁止通过 USB 安装"列表里，开关打开。

按铁律 5（不向用户正在使用的设备注入输入），我没有点击任何对话框、没有重试安装、没有改任何设置。另外，本测试包安装本身就需要人在设备上点"继续安装"（MIUI USB 安装确认属于安全门），这一步应由用户点击，不应由代理注入。

## 正式包与设备状态（前后一致）

| 项 | 开始（22:40） | 结束（22:45） |
|---|---|---|
| com.pudutech.attention versionCode | 1（minSdk 24，targetSdk 36） | 1 |
| lastUpdateTime | 2026-09-22 23:15:55 | 2026-09-22 23:15:55 |
| signatures | PackageSignatures{9fa9ebe version:2, signatures:[51ed3f60]} | 相同 |

`diff` 结果：IDENTICAL。对正式包只执行过 `dumpsys package` 这一种只读命令。

- 测试包 `com.pudutech.attention.wac`：安装失败，设备上不存在（`pm list packages` 计数 0），无需卸载。
- 设备设置：accelerometer_rotation=1、user_rotation=0、font_scale=1.0、开发者选项=1、三项动画缩放=1、show_ime_with_hard_keyboard=1、默认输入法 com.typeless.mobile、screen_off_timeout=600000、stay_on_while_plugged_in=0；结束时与开始时 `diff` 一致。本步未改任何设置。

## 已完成的准备（恢复时可直接用）

- APK：`.worktrees/wac-063/apps/attention-android/android/app/build/outputs/apk/release/app-release.apk`（25 MB，arm64-v8a，JS bundle 已打包进 APK）
  - 构建命令：`./gradlew :app:assembleRelease -PattentionAllowDebugReleaseSigning=true -PreactNativeArchitectures=arm64-v8a`，已显式 unset `ATTENTION_RELEASE_*`；BUILD SUCCESSFUL，耗时 2m31s
  - aapt2：`package name='com.pudutech.attention.wac'`，标签 `balen-bot 测试wac063`
  - apksigner：证书是 `CN=Android Debug`，SHA-256 fac61745…9c；正式包是 51ed3f60 签名，两者不同，所以不可能误覆盖正式包
  - 临时未提交改动只有 3 行：applicationId 加 `.wac`；app_name 改成测试名；深链 scheme 改成 `attention-wac063-test`，避免 `attention://request` 在正式包和测试包之间弹出选择框、劫持正式包深链
  - 源码中没有硬编码生产域名（grep 过 balen./jp-bot/https://，只有示例占位）；没有 google-services.json，因此 FCM 插件未启用
- 单元侧佐证（非真机）：layoutClass、tabletNavigation、tabletSourceGuard、contentWidth、bottomSheet、rootTabs、signalsScreen 共 7 个套件、48 个用例，连续 3 次全过，没有靠重试（`jest-t7a.txt`）

## 上机时逐项期望值（从源码读出，供恢复时对照）

dp 换算：竖屏 2272/2.5 = 908.8dp；横屏 3408/2.5 = 1363.2dp；横屏左右半分屏约 680dp（扣除分隔条）；1/3 分屏约 450dp。`useLayoutClass` 阈值是 600/840，`>=` 为界。

| 窗口 | dp | 档位 | 导航 | 单列页（Setup/Settings/TradingSetup） | 信号页 |
|---|---|---|---|---|---|
| 竖屏 | 909 | expanded | 左侧栏，图标旁带标签 | 居中，maxWidth 720 | 左栏 400dp 列表 + 右栏（maxWidth 840）"选择一条记录查看详情" |
| 横屏 | 1363 | expanded | 左侧栏 | 居中 720 | 双栏，右栏限宽 840 |
| 横屏半屏 | 约 680 | medium | 左侧栏 | 居中 720（窗口不到 720 时等于通栏） | 单栏；点选后 push 详情，返回键关闭 |
| 1/3 屏 | 约 450 | compact | 底部 Tab | 通栏 | 单栏 |

- 冷启动流程：新装先到 NEEDS_SETUP，Home 会 reset 到"服务连接"页。要进 Tab，需要在该页填 `https://wac-test.invalid` 和随机测试值：保存时 configure 先成功，registerDevice 随后失败并弹"连接失败"；重启测试包后状态为 READY，就能进 Tab。release 构建只接受 https。
- 空态与错误态期望：
  - 信号页：Trading 未配置时状态条显示"信号 · 接口失败"，列表显示"交易服务未配置"
  - Trading 配置成 `https://wac-test.invalid` 后：信号页显示"采集服务不可达"；账户页显示红字错误、freshness 条、`合计` 卡片、"查看持仓"
  - 持仓页显示"持仓加载失败"
  - 设置页采集服务显示"交易服务未配置"或"采集服务不可达"
  - 告警页在线/离线徽标
- 已知预期差：账户卡两列或四列、仓位双栏属于 A-T（看板 pending），第①步只记录单列现状，不判 FAIL。
- 横屏键盘风险点：targetSdk 36 在 Android 17 上强制 edge-to-edge，`adjustResize` 可能不再缩窗口。Setup 用的是 `KeyboardAvoidingView behavior={undefined}`（Android），Settings 的 TradingConfigForm 外层只有 ScrollView，没有键盘避让。这正是需要在真机上验证的地方。
- 抽屉（BottomSheet，maxWidth 640、高度上限 88%）只挂在仓位详情页，需要仓位数据，第①步够不到，转第②步。

## 恢复条件与建议

1. 请用户确认平板上的 pad-read 安装或设置操作已结束，设备空闲。
2. 由用户在平板上对 `balen-bot 测试wac063` 的"USB 安装提示"亲手点"继续安装"（Tester 重新发起 `adb install` 后，弹窗有约 10 秒倒计时，默认拒绝）。
3. 之后 Tester 按上表跑完第①步。APK 不需要重建，除非集成分支 HEAD 变化。

## 证据文件

`/private/tmp/claude-501/-Users-balen-projects-trader-bot/e75fc499-c012-4132-9bf5-78b4f2f83148/scratchpad/wac063/`：
`official-before.txt`、`official-after.txt`、`settings-before.txt`、`settings-after.txt`、`build.log`、`jest-t7a.txt`、`00-install-prompt.png`、`00b-focus-now.png`（两张图都只有系统设置页，没有私人内容）
