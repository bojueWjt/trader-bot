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
| 十审 | fail | closed 3（S30、A23-A/C/L）/ partial-P2 2 / open 8（S29、S31、S38、S39 阻断；S32、S33、S35、S37 同族未归一） | Codex job `task-mtwgpvh7-bifmsn` rawOutput（原文"十审终裁：fail"；文件 828 行，九审正文 339 行逐字保留，末尾四行符合 A21 追加制）；`taskList.json` 看板 note |
| 十一审 | fail | 十审指定修复项**全部 closed**（S29/S31/S32/S33/S35/S37/S38/S39）；新增 open 2（S40、S41），两条均为 **A24 门自身的漏检**而非被守护代码的缺陷 | Codex job `task-mtwhxe7y-rc756b` rawOutput（原文"十一审终裁：fail"；文件 1678 行，十审段 487 行、九审段 339 行**经我逐行核对逐字保留**，末六行三组结尾符合 A21 追加制）；`taskList.json` 看板 note |
| 十二审 | fail | 十一审指定项**全部 closed**（S40、S41、B19 `force_close_net_R`、A24 集合双向与使用次数、P1–P8 各模式自身可失败、A28 十条差分能失败）；新增 open 2（**S42** A28 离网生成器与三道门漏检、**S43** 消费接缝把离网行冒充完整网格）。**S43 是真实行为缺陷，不只是门的缺口。** | Codex job `task-mtwkpm4k-me6mtf` rawOutput（原文"十二审终裁：fail"；文件 9840 行，历史正文及各轮尾行完整保留，源码与测试 SHA256 及 mtime 均未变）；131 组三阶段突变取证；`taskList.json` 看板 note |
| 十三审 | fail | 十二审指定项**全部 closed**（S42、S43 原反例闭合）；新增 open 2——**S44** 我的快照护栏漏检批中新增文件（单向比对）、**S45** 分区消费方缺少窗口内 −1µs 对抗输入（1µs 前向容差突变下三道门全绿）。**S44 是我自己工具的缺陷，已由我当场修复并三向自证**（新增/删除/干净）。 | Codex job `task-mtwm08j6-54w9yy` rawOutput（原文"十三审终裁：fail"；97 次三阶段突变实验；历史字节完整保留）；`taskList.json` 看板 note |
| **十四审** | **pass** | **P1 阻断项：无。** S44/S45 闭合；A36 四条对角线、A37 十项优先级回归均闭合。103 组独立磁盘突变；29 个新增/修改测试节点**均证明可失败**、还原 SHA256 相等。**审查方未修改产品代码与测试。** | Codex job `task-mtwnbrdd-ez4zcr` rawOutput（原文"十四审终裁：pass"；文件 24285 行，历史正文逐字保留，六组结尾按时序）；M-10 看板 verify 原样实跑 **rc=0**；`taskList.json` 看板 note |

## 为什么需要这份台账（2026-09-11 的两次实际丢失）

1. **我的突变自证脚本用了不可诊断的还原检查**：以"还原后 rc 是否为 1"判断文件是否复原，而文件被正确还原（终裁 fail）与文件被毁（找不到终裁行）**都得 rc=1**。结果八审终裁行在一次还原中丢失，脚本报告"已还原 ✓"。已改为内容 SHA256 比对。教训与 S21 同族：**一个无法区分成功与失败的检查，比没有检查更坏**。
2. **我给九审的任务书要求"全文件只允许一条以终裁结尾的行"**——这条是早期为了让 verify 判读无歧义而加的，但它**强制每轮删掉上一轮终裁**，与 A21「只追加不覆盖」直接冲突。G0 探测到的"318 行、零终裁"正是九审删旧未写新的窗口；而 `c14eee7` 入库的也恰是这一状态，**git 并未保住八审终裁**（只保住了正文）。

**修正**：此后任务书改为「新一轮终裁**追加**文末，旧轮终裁原样保留并以轮次名区分」。M-10 的 verify 用 `tail -1` 取最后一条，本就兼容多条终裁行，无需改动即可配合 A21。
