# review-G2-P1 各轮终裁台账（只追加，不覆盖）

> 契约 §9 A21：审查报告只追加、不覆盖历史轮次判定文字；覆盖一份记录之前，先让它可恢复。
> 本文件是终裁行的**独立台账**——`review-G2-P1.md` 被每轮重写时，此处不受影响。
> 每条附**记录来源**，以便在正文丢失时仍可追溯。verify 仍按 §9.9 A7 读 `review-G2-P1.md` 的最后一条终裁行，本文件不参与判读。

| 轮次 | 终裁 | 计数 | 记录来源 |
|---|---|---|---|
| 一审 | fail | 必修 S01–S13（closed 0 / partial 0 / open 13） | Codex job `task-mtvvxx98-tr4r8e` rawOutput；`review-G2-P1.rounds-1-2.md` 快照（sha256 `15a7f2ce…`） |
| 二审 | fail | closed 1 / partial 12 / open 0，新增 S14 | Codex job `task-mtvwu101-31aoet` rawOutput；同上快照 |
| 三审 | fail | closed 4 / partial-P2 8 / open 2（S05、S14），新增 S15、S16 | Codex job `task-mtvxpb1w-n3flh9` rawOutput |
| 四审 | fail | closed 6 / partial-P2 9 / open 2（S05、S17），新增 S17 | Codex job `task-mtwbpr2b-jlo7je` rawOutput |
| 五审 | fail | closed 9 / partial-P2 8 / open 2，新增 S18、S19 | Codex job `task-mtwcwmrq-4ac18i` rawOutput |
| 六审 | fail | closed 12 / partial-P2 8 / open 2，新增 S22 | Codex job `task-mtwdgqyp-dcxdc0` rawOutput |
| 七审 | fail | closed 15 / partial-P2 8 / open 2，新增 S24、S25 | Codex job `task-mtwe76h5-u96l6s` rawOutput |
| **八审** | **fail** | closed 18 / partial-P2 8 / open 2，新增 S27、S28 | Codex job `task-mtwf0gbr-4vtswg` rawOutput（原文"八审终裁：fail"，文件 319 行）；`taskList.json` 看板 note |
| 九审 | fail | closed 21 / partial-P2 8 / open 3（S29、S30、S31；G2-SC-01 已判 closed） | Codex job `task-mtwfvpbz-bg3a6m` rawOutput（原文"九审终裁：fail"，339 行、声明与终裁各唯一、无未完成标记）；`taskList.json` 看板 note |

## 为什么需要这份台账（2026-09-11 的两次实际丢失）

1. **我的突变自证脚本用了不可诊断的还原检查**：以"还原后 rc 是否为 1"判断文件是否复原，而文件被正确还原（终裁 fail）与文件被毁（找不到终裁行）**都得 rc=1**。结果八审终裁行在一次还原中丢失，脚本报告"已还原 ✓"。已改为内容 SHA256 比对。教训与 S21 同族：**一个无法区分成功与失败的检查，比没有检查更坏**。
2. **我给九审的任务书要求"全文件只允许一条以终裁结尾的行"**——这条是早期为了让 verify 判读无歧义而加的，但它**强制每轮删掉上一轮终裁**，与 A21「只追加不覆盖」直接冲突。G0 探测到的"318 行、零终裁"正是九审删旧未写新的窗口；而 `c14eee7` 入库的也恰是这一状态，**git 并未保住八审终裁**（只保住了正文）。

**修正**：此后任务书改为「新一轮终裁**追加**文末，旧轮终裁原样保留并以轮次名区分」。M-10 的 verify 用 `tail -1` 取最后一条，本就兼容多条终裁行，无需改动即可配合 A21。
