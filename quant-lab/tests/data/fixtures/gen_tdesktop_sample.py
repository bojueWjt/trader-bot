#!/usr/bin/env python3
"""合成 TDesktop 导出夹具生成器（确定性，seed=20260911）。运行：python tests/data/fixtures/gen_tdesktop_sample.py

产出 tests/data/fixtures/tdesktop_sample/：
  AlphaSignals/result.json  舒琴式结构化信号频道（五反例锚点 A#120/130/140/150-154/160）
  BetaTrades/result.json    Titan/Gauls 式图文频道（相册、纯图、编辑、转发）
  GammaRelay/result.json    搬运频道（转发、无头复制、近似复制、精确重复、服务消息、坏时间、缺图、未知字段）
  DeltaLive/live.jsonl      Telethon 实收快照（V 级；同一消息两次观察=编辑前后；反例 1 的实收版本）
全部为虚构数据，无真实频道/账号。锚点 id 见 ANCHORS。
"""
from __future__ import annotations

import json
import pathlib
import random
import struct
import zlib
from datetime import UTC, datetime, timedelta

ROOT = pathlib.Path(__file__).parent / "tdesktop_sample"
rng = random.Random(20260911)

CH = {
    "A": {"id": 2000000001, "name": "Alpha Signals", "dir": "AlphaSignals"},
    "B": {"id": 2000000002, "name": "Beta Trades", "dir": "BetaTrades"},
    "C": {"id": 2000000003, "name": "Gamma Relay", "dir": "GammaRelay"},
}
PEER = {k: -(1_000_000_000_000 + v["id"]) for k, v in CH.items()}
PEER["D"] = PEER["A"]  # V sidecar is the same author namespace, not a fourth channel


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def png(seed: int) -> bytes:
    from draw_numbers import draw
    return draw([str(seed), "100", "90"], decoration=seed)[0]


class Chan:
    def __init__(self, key: str):
        self.key = key
        self.meta = CH[key]
        self.msgs: list[dict] = []
        self.next_id = 100
        self.dir = ROOT / self.meta["dir"]
        (self.dir / "photos").mkdir(parents=True, exist_ok=True)
        self.photo_n = 0

    def add(self, when: datetime, text="", *, mid=None, edited: datetime | None = None, reply=None, fwd=None, fwd_id=None,
            fwd_mid=None, photo=True if False else None, photos=0, grouped=None, service=None, extra=None, missing_photo=False,
            bad_time=False, entities=None) -> int:
        mid = mid if mid is not None else self.next_id
        self.next_id = max(self.next_id, mid) + 1
        m = {"id": mid, "type": "service" if service else "message", "date": when.strftime("%Y-%m-%dT%H:%M:%S"),
             "date_unixtime": str(int(when.timestamp()))}
        if bad_time:
            m["date_unixtime"] = "n/a"
        if service:
            m.update({"actor": self.meta["name"], "actor_id": f"channel{self.meta['id']}", "action": service, "title": self.meta["name"]})
        else:
            m.update({"from": self.meta["name"], "from_id": f"channel{self.meta['id']}"})
        if edited:
            m["edited"] = edited.strftime("%Y-%m-%dT%H:%M:%S")
            m["edited_unixtime"] = str(int(edited.timestamp()))
        if reply is not None:
            m["reply_to_message_id"] = reply
        if fwd:
            m["forwarded_from"] = fwd
            if fwd_id is not None:
                m["forwarded_from_id"] = fwd_id
                m["forwarded_from_message_id"] = fwd_mid
        if photos:
            self.photo_n += 1
            name = f"photos/photo_{mid}@{when.strftime('%d-%m-%Y_%H-%M-%S')}.png"
            if not missing_photo:
                (self.dir / name).write_bytes(png(self.meta["id"] % 1000 + self.photo_n))
            m["photo"] = name
            m["photo_file_size"] = 120
            m["width"] = 256
            m["height"] = 80
        if grouped is not None:
            m["grouped_id"] = grouped
        m["text"] = entities if entities is not None else text
        m["text_entities"] = []
        if extra:
            m.update(extra)
        self.msgs.append(m)
        return mid

    def album(self, when: datetime, caption: str, n=3, explicit_gid: int | None = None) -> list[int]:
        ids = []
        for i in range(n):
            ids.append(self.add(when, caption if i == 0 else "", photos=1, grouped=explicit_gid))
        return ids

    def write(self, exported_at: str = "2025-03-01T00:00:00+00:00"):
        doc = {"name": self.meta["name"], "type": "public_channel", "id": self.meta["id"], "messages": self.msgs}
        (self.dir / "result.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1))
        (self.dir / "export_manifest.json").write_text(json.dumps({"exported_at": exported_at, "tool": "synthetic-tdesktop", "note": "导出快照时刻=H1 版本首次可证明完整可见的时刻"}, ensure_ascii=False, indent=1))


def shuqin(sym, side, lo, hi, tps, sl, lev=10, note="支撑位上方做反弹"):
    tp = " ".join(f"点位{i+1}：{t}附近" for i, t in enumerate(tps))
    verb = "跌破" if side == "做多" else "涨破"
    return f"{sym}\n方向：{side}\n入场：{lo}-{hi}附近\n信心度：中\n倍数：{lev}倍\n仓位：10%\n止盈：{tp}\n止损：小幅{verb}{sl}一点。\n理由：{note}\n注：挂单不要挂整数位，难以成交。"


def titan(sym, e1, e2, sl, side="LONG"):
    return f"我的 #{sym} {'买入' if side=='LONG' else '做空'}区域\n\n👉 首次入场价：> ({e1})\n👉 第二次入场价：> ({e2})\n\n🚨 我的止损价：({sl}) 美元 区域 (严格的风险管理)。\n\n👉 我将在每次止盈 (TP) 时锁定 25% 的利润。"


# 每频道各自的闲聊/分析/结果语料（互不相同，避免跨频道"同文"全是语料造成的假复制组；跨频道复制只来自显式搬运锚点）
POOLS = {
    "A": {
        "chatter": ["大家早上好，今天继续看盘。", "行情震荡，耐心等待信号。", "感谢大家的支持 ❤️", "周末愉快，注意风控。", "美联储数据今晚公布，注意波动。",
                    "有问题的私信我。", "今天不做单，观望。", "昨天的单子大家都拿住了吗？", "记住：仓位管理永远第一。", "新人先看置顶。"],
        "analysis": ["BTC 4小时级别仍在区间内震荡，上方压力 66000，下方支撑 60000，突破再看方向。", "ETH/BTC 汇率走弱，山寨季还早。",
                     "SOL 日线三连阳，但成交量没跟上，谨慎追高。", "原油昨晚大跌，风险资产同步承压。", "美股财报季，币圈流动性被抽。"],
        "results": ["昨天 BTC 多单已止盈全部离场，恭喜跟上的朋友。", "ETH 空单止损离场，-1.2%，接受。", "SOL 多单 TP2 到达，剩余仓位保本止损。",
                    "本周战绩：3 胜 1 负。", "止盈截图见上，感谢信任。"],
    },
    "B": {
        "chatter": ["GM AREAN 🦅 今天也要守纪律。", "市场没有给出明确结构，我选择等待。", "感谢每一位坚持学习的成员 ❤️‍🔥", "周末不交易，陪家人。",
                    "CPI 数据前不开新仓。", "问题请在评论区留言，我会统一回复。", "今天休息，不开单。", "昨晚的计划还在有效期内。", "风险管理永远排在利润前面。", "新成员请先读频道简介。"],
        "analysis": ["$BTC 在 4H 图上形成更高低点，只要守住 64k 支撑，多头结构不变。", "$ETH 相对 $BTC 走弱，暂不考虑山寨。",
                     "$SOL 日线放量突破，但我要等回踩确认。", "宏观：原油大跌拖累风险偏好。", "美股财报季资金外流，谨慎。"],
        "results": ["$BTC 多单三个目标全部达成 ✅", "$ETH 空单止损出局，-1.2%，按计划执行。", "$SOL 多单 TP2 达成，止损移至入场。",
                    "本周：3 胜 1 负，净 +4.1R。", "止盈截图如图。"],
    },
    "C": {
        "chatter": ["早安，今日汇总稍后发布。", "各频道暂无新信号，继续等待。", "感谢关注本汇总频道。", "周末信号稀少，注意休息。", "宏观数据夜，各家都在观望。",
                    "搬运有延迟，请以原频道为准。", "今日无更新。", "昨天的信号有人跟吗？", "提醒：本频道不提供建议。", "新人请看置顶说明。"],
        "analysis": ["汇总：多家频道认为 BTC 在 60000-66000 区间内震荡。", "汇总：ETH 相对弱势，各家暂不推荐山寨。",
                     "汇总：SOL 放量上涨但多数频道建议等回踩。", "汇总：原油暴跌，风险资产承压。", "汇总：财报季流动性紧张。"],
        "results": ["汇总：Alpha 昨日 BTC 多单止盈。", "汇总：Beta ETH 空单止损。", "汇总：SOL 多单 TP2 到达。", "汇总：本周各家整体盈利。", "汇总：止盈截图见原频道。"],
    },
}


def pool(key: str, *names: str) -> list[str]:
    out: list[str] = []
    for n in names:
        out += POOLS[key][n]
    return out


ANCHORS: dict[str, int] = {}


def build_alpha() -> Chan:
    a = Chan("A")
    t = ts("2024-03-01T09:00:00")
    a.add(t, service="create_channel")
    a.add(t + timedelta(minutes=1), "欢迎来到 Alpha Signals，本频道信号仅供研究。")
    a.add(t + timedelta(minutes=2), service="pin_message", extra={"message_id": 101})
    # 常规信号 + 管理（2024-03）
    m = a.add(ts("2024-03-04T08:30:00"), shuqin("ETH", "做多", 3295, 3315, [3360, 3430, 3530], 3268))
    ANCHORS["A_eth_1"] = m
    a.add(ts("2024-03-04T14:10:00"), "ETH 到达点位1，止盈一半，剩余仓位止损上移到入场。", reply=m)
    a.add(ts("2024-03-05T02:00:00"), "ETH 点位2 到达，全部止盈离场。恭喜。", reply=m)
    a.add(ts("2024-03-06T10:00:00"), rng.choice(pool("A", "chatter")))
    m = a.add(ts("2024-03-08T09:00:00"), shuqin("BTC", "做空", "6.48", "6.53", ["6.41", "6.36", "6.27"], "6.57", note="6.52万附近颈线阻力"))
    ANCHORS["A_btc_short_wan"] = m
    a.add(ts("2024-03-08T18:00:00"), "BTC 空单止损离场。", reply=m)
    for i in range(6):
        a.add(ts("2024-03-10T10:00:00") + timedelta(days=i, hours=rng.randint(0, 9)), rng.choice(pool("A", "chatter", "analysis")))
    # 反例 1：编辑 SL 不回填（10:00 发布，10:20 编辑改 SL；导出只见最终版）
    m = a.add(ts("2024-04-02T10:00:00"), shuqin("BTC", "做多", 62000, 62500, [63500, 64500, 66000], 60800), mid=120, edited=ts("2024-04-02T10:20:00"))
    ANCHORS["CE1_edited_sl"] = m
    a.add(ts("2024-04-02T20:00:00"), "BTC 多单到达点位1。", reply=120)
    for i in range(5):
        a.add(ts("2024-04-05T10:00:00") + timedelta(days=i), rng.choice(pool("A", "chatter", "results")))
    # 反例 2：未成交触 TP 不闭仓（限价远低于市价，随后作者声称到达止盈）
    m = a.add(ts("2024-05-06T08:00:00"), shuqin("ETH", "做多", 2900, 2905, [3000, 3080, 3150], 2850, note="回踩 2900 支撑再进"), mid=130)
    ANCHORS["CE2_unfilled_tp"] = m
    a.add(ts("2024-05-06T11:00:00"), "ETH TP1 到了！", reply=130, mid=131)  # ADR 原反例：结果帖，不改状态
    a.add(ts("2024-05-06T12:00:00"), "ETH 到达 3000，止盈一半！", reply=130, mid=132)  # 声称减量：C 保持
    for i in range(5):
        a.add(ts("2024-05-08T10:00:00") + timedelta(days=i), rng.choice(pool("A", "chatter", "analysis")))
    # 反例 3：超时只过期
    m = a.add(ts("2024-05-20T09:00:00"), shuqin("SOL", "做空", 150, 152, [145, 140, 132], 156, note="150 上方压力") + "\n本单 24 小时内有效，未成交自动作废。", mid=140)
    ANCHORS["CE3_timeout"] = m
    for i in range(5):
        a.add(ts("2024-05-22T10:00:00") + timedelta(days=i), rng.choice(pool("A", "chatter", "results")))
    # 反例 4：同向双单不误并（72h 内两单 BTC 多）
    m1 = a.add(ts("2024-06-10T09:00:00"), shuqin("BTC", "做多", 60000, 60300, [61000, 62000, 63500], 59200), mid=150)
    a.add(ts("2024-06-10T15:00:00"), rng.choice(pool("A", "analysis")), mid=151)
    m2 = a.add(ts("2024-06-11T15:00:00"), shuqin("BTC", "做多", 58500, 58800, [59500, 60500, 62000], 57800, note="第二单，独立仓位，与上一单不冲突"), mid=152)
    a.add(ts("2024-06-12T08:00:00"), "第一单 BTC 多止损上移到 60000 保本。", reply=150, mid=153)
    a.add(ts("2024-06-12T20:00:00"), "第二单到达点位1，止盈一半。", reply=152, mid=154)
    a.add(ts("2024-06-13T09:00:00"), "BTC 多单止损上移到 59500。", mid=155)  # 无指针：两个同向根都在 72h 内 → 必须 unresolved
    ANCHORS["CE4_ambiguous_mgmt"] = 155
    ANCHORS["CE4_dual_1"] = m1
    ANCHORS["CE4_dual_2"] = m2
    for i in range(4):
        a.add(ts("2024-06-14T10:00:00") + timedelta(days=i), rng.choice(pool("A", "chatter")))
    # 反例 5：提前管理留缺入口（无前置提议）
    m = a.add(ts("2024-07-01T10:00:00"), "BTC 空单止损移到 65000，保护利润。", mid=160)
    ANCHORS["CE5_orphan_mgmt"] = m
    a.add(ts("2024-07-01T18:00:00"), "BTC 空单全部止盈离场。", reply=160, mid=161)
    # 跨年信号（2025）
    for i, (sym, lo, hi, tps, sl) in enumerate([("SOL", 185, 188, [195, 205, 220], 178), ("ETH", 3050, 3080, [3150, 3250, 3400], 2990), ("BTC", 94000, 94500, [96000, 98000, 101000], 92500)]):
        m = a.add(ts("2025-01-06T09:00:00") + timedelta(days=3 * i), shuqin(sym, "做多", lo, hi, tps, sl))
        a.add(ts("2025-01-06T21:00:00") + timedelta(days=3 * i), rng.choice(["到达点位1，止盈一半。", "止损离场。", "止盈全部离场。"]), reply=m)
    ANCHORS["A_2025_first"] = m - 4
    a.add(ts("2025-01-20T09:00:00"), "美股开盘前，先观望。")
    a.add(ts("2025-01-21T09:00:00"), shuqin("GOOGL", "做多", 341, 342, [348, 357, 365], 337, note="谷歌 340 二次探底"))
    for i in range(8):
        a.add(ts("2025-01-22T10:00:00") + timedelta(days=i), rng.choice(pool("A", "chatter", "analysis", "results")))
    return a


def build_beta() -> Chan:
    b = Chan("B")
    b.add(ts("2024-03-02T12:00:00"), service="create_channel")
    b.add(ts("2024-03-02T12:01:00"), "Welcome to Beta Trades 🦅 教学与研究用途。")
    # 相册（推断式，无 grouped_id）+ 说明
    ids = b.album(ts("2024-03-05T14:00:00"), titan("ZRO", 0.7102, 0.688, 0.6666), n=3)
    ANCHORS["B_album_inferred"] = ids[0]
    b.add(ts("2024-03-06T09:00:00"), "#ZRO 第一目标到达，锁定 25% 利润。", reply=ids[0])
    # 相册（显式 grouped_id）
    ids = b.album(ts("2024-03-09T10:00:00"), titan("FIL", 6.328, 6.156, 5.998), n=2, explicit_gid=13000000001)
    ANCHORS["B_album_explicit"] = ids[0]
    # 纯图（无文字）
    m = b.add(ts("2024-03-12T08:00:00"), "", photos=1)
    ANCHORS["B_pure_image"] = m
    b.add(ts("2024-03-12T08:01:00"), "如图，BTC 关键位。", reply=m)
    # 图文信号 + 编辑
    m = b.add(ts("2024-03-20T11:00:00"), titan("BTC", 64570, 64900, 66200, side="SHORT"), photos=1, edited=ts("2024-03-20T11:45:00"))
    ANCHORS["B_edited_photo_signal"] = m
    b.add(ts("2024-03-21T09:00:00"), "我的 $BTC 交易更新 🔥\n\n价格在该区间受到阻力，并下跌了近 1.6%。✅\n👉 现在锁定 10% 的利润，并将我的止损位调整到 65000。", reply=m)
    # 转发自 Alpha（带 id 指针）
    b.add(ts("2024-03-22T10:00:00"), shuqin("BTC", "做空", "6.48", "6.53", ["6.41", "6.36", "6.27"], "6.57", note="6.52万附近颈线阻力"), fwd="Alpha Signals", fwd_id=PEER["A"], fwd_mid=ANCHORS["A_btc_short_wan"])
    for i in range(10):
        b.add(ts("2024-04-01T10:00:00") + timedelta(days=2 * i, hours=rng.randint(0, 12)), rng.choice(pool("B", "chatter", "analysis")), photos=1 if i % 3 == 0 else 0)
    # Gauls 风格
    m = b.add(ts("2024-05-02T10:00:00"), "$ETHFI 购买策略\n\n入场价：当前价格和 3612\n目标价：4542\n止损价：3446\n\n我认为前景很好。")
    ANCHORS["B_ethfi"] = m
    b.add(ts("2024-05-05T10:00:00"), "$ETHFI 三天前的多单 交易更新：\n\n👉 价格上涨了 7%。\n👉 此时继续锁定利润，并将止损位调整到 0.3788。", reply=m)
    b.add(ts("2024-05-07T10:00:00"), "$TIA 交易更新：\n\n👉 在止损位附近平仓，尽量减少损失。\n👉 亏损 -3%。")
    for i in range(12):
        b.add(ts("2024-06-01T10:00:00") + timedelta(days=3 * i), rng.choice(pool("B", "chatter", "results", "analysis")), photos=1 if i % 4 == 0 else 0)
    ids = b.album(ts("2024-08-15T10:00:00"), titan("SOL", 142, 138, 131), n=2)
    for i in range(13):
        b.add(ts("2024-09-01T10:00:00") + timedelta(days=4 * i), rng.choice(pool("B", "chatter", "analysis")))
    b.add(ts("2025-01-10T10:00:00"), titan("ETH", 3210, 3150, 3040), photos=1)
    b.add(ts("2025-01-12T10:00:00"), "#ETH 第一目标到达 ✅")
    for i in range(6):
        b.add(ts("2025-01-14T10:00:00") + timedelta(days=i), rng.choice(pool("B", "chatter", "results")))
    return b


def build_gamma(alpha: Chan) -> Chan:
    c = Chan("C")
    c.add(ts("2024-03-03T08:00:00"), service="create_channel")
    c.add(ts("2024-03-03T08:00:30"), "搬运各大频道信号，仅作汇总。")
    c.add(ts("2024-03-03T08:01:00"), service="pin_message", extra={"message_id": 101})
    a_by_id = {m["id"]: m for m in alpha.msgs}
    # 转发（有头）
    m = c.add(ts("2024-03-04T08:35:00"), a_by_id[ANCHORS["A_eth_1"]]["text"], fwd="Alpha Signals", fwd_id=PEER["A"], fwd_mid=ANCHORS["A_eth_1"])
    ANCHORS["C_fwd_of_A_eth_1"] = m
    # 无头复制（精确文本）
    m = c.add(ts("2024-03-08T09:07:00"), a_by_id[ANCHORS["A_btc_short_wan"]]["text"])
    ANCHORS["C_copy_of_A_btc_short"] = m
    # 近似复制（改动几个字）
    m = c.add(ts("2024-04-02T10:30:00"), a_by_id[120]["text"].replace("注：挂单不要挂整数位，难以成交。", "（搬运自 Alpha）"))
    ANCHORS["C_near_copy_CE1"] = m
    # 精确重复（同频道两次）
    dup = "BTC 突破 66000 了，注意追多风险。"
    m = c.add(ts("2024-04-10T10:00:00"), dup)
    ANCHORS["C_dup_first"] = m
    m = c.add(ts("2024-04-10T10:00:05"), dup)
    ANCHORS["C_dup_second"] = m
    # 坏时间（date_unixtime 不可解析）
    m = c.add(ts("2024-04-15T10:00:00"), "这条消息的时间戳损坏。", bad_time=True)
    ANCHORS["C_bad_time"] = m
    # 缺图
    m = c.add(ts("2024-04-18T10:00:00"), "图片没导出来的一条。", photos=1, missing_photo=True)
    ANCHORS["C_missing_photo"] = m
    # 未知字段（schema drift）
    m = c.add(ts("2024-04-20T10:00:00"), "带未知字段的消息。", extra={"reactions_extra": {"👍": 3}})
    ANCHORS["C_unknown_key"] = m
    # 实体文本（list 形式）
    m = c.add(ts("2024-04-22T10:00:00"), entities=[{"type": "bold", "text": "BTC"}, " 方向：做多 入场：", {"type": "code", "text": "61000-61200"}, " 止损 60000 止盈 63000"])
    ANCHORS["C_entities"] = m
    for i in range(20):
        c.add(ts("2024-05-01T10:00:00") + timedelta(days=4 * i, hours=rng.randint(0, 20)), rng.choice(pool("C", "chatter", "analysis", "results")))
    # 搬运 CE4 两单（无头复制）
    c.add(ts("2024-06-10T09:20:00"), a_by_id[150]["text"])
    c.add(ts("2024-06-11T15:20:00"), a_by_id[152]["text"])
    for i in range(12):
        c.add(ts("2024-08-01T10:00:00") + timedelta(days=5 * i), rng.choice(pool("C", "chatter", "analysis")))
    c.add(ts("2025-01-06T09:30:00"), a_by_id[ANCHORS["A_2025_first"]]["text"], fwd="Alpha Signals")
    for i in range(14):
        c.add(ts("2025-01-08T10:00:00") + timedelta(days=i), rng.choice(pool("C", "chatter", "results")))
    return c


def build_delta():
    """Telethon 实收快照：反例 1 的 V 级版本链 —— 10:00 发布(SL 61000) 10:25 首次实收；10:20 的编辑(SL 60800) 于 10:26 实收。"""
    d = ROOT / "DeltaLive"
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    base = ts("2024-04-02T10:00:00")

    def line(mid, when: datetime, text, first_seen: datetime, edit: datetime | None = None, **kw):
        rec = {"peer_id": PEER["D"], "peer_name": "Delta Live", "id": mid - 435, "date": int(when.timestamp()), "message": text,
               "first_seen_at": first_seen.isoformat(), "snapshot_at": first_seen.isoformat(), "cohort": "delta-watch-2024Q2"}
        if edit:
            rec["edit_date"] = int(edit.timestamp())
        rec.update(kw)
        lines.append(rec)

    sig_v1 = shuqin("BTC", "做多", 62000, 62500, [63500, 64500, 66000], 61000)
    sig_v2 = shuqin("BTC", "做多", 62000, 62500, [63500, 64500, 66000], 60800)
    line(500, base, sig_v1, base + timedelta(minutes=25))
    line(500, base, sig_v2, base + timedelta(minutes=26), edit=base + timedelta(minutes=20))
    ANCHORS["D_live_edit_v1_v2"] = 65
    line(501, base + timedelta(hours=10), "BTC 多单到达点位1。", base + timedelta(hours=10, seconds=3), reply_to_msg_id=65)
    line(502, base + timedelta(days=1), "今天观望。", base + timedelta(days=1, seconds=2))
    line(503, base + timedelta(days=2), shuqin("ETH", "做空", 3400, 3420, [3300, 3200], 3470), base + timedelta(days=2, seconds=4))
    line(504, base + timedelta(days=2, hours=6), "ETH 空单止损离场。", base + timedelta(days=2, hours=6, seconds=2), reply_to_msg_id=68)
    line(505, base + timedelta(days=3), "周末愉快。", base + timedelta(days=3, seconds=1))
    (d / "live.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in lines) + "\n")


def main():
    import shutil

    rng.seed(20260911)
    ANCHORS.clear()

    if ROOT.resolve() != (pathlib.Path(__file__).parent / "tdesktop_sample").resolve():
        raise ValueError("fixture generator is restricted to its test directory")
    if ROOT.exists():
        shutil.rmtree(ROOT)
    a = build_alpha()
    b = build_beta()
    c = build_gamma(a)
    channels = (a, b, c)
    id_maps = {ch.key: {old: i + 1 for i, old in enumerate(sorted({m["id"] for m in ch.msgs}))} for ch in channels}
    for name, old in list(ANCHORS.items()):
        key = name[0]
        if name.startswith("CE"):
            key = "A"
        ANCHORS[name] = id_maps[key][old]
    matrix = {}
    for ch in channels:
        for m in ch.msgs:
            m["id"] = id_maps[ch.key][m["id"]]
            if m.get("reply_to_message_id") in id_maps[ch.key]:
                m["reply_to_message_id"] = id_maps[ch.key][m["reply_to_message_id"]]
            for origin, peer in PEER.items():
                if origin in id_maps and m.get("forwarded_from_id") == peer:
                    m["forwarded_from_message_id"] = id_maps[origin].get(m.get("forwarded_from_message_id"), m.get("forwarded_from_message_id"))
        occupied = {m["id"] for m in ch.msgs}
        if ch.key == "A":
            occupied.update(range(65, 71))
        for mid in range(1, 73):
            if mid in occupied:
                continue
            text = f"{ch.key} 合成观察 {mid}，暂不交易。"
            extra = {}
            if mid == 62:
                text = "BTC 做多 入场 60000 止损 59000；ETH 做空 入场 3400 止损 3500"
            if mid in (63, 64, 65):
                text = "BTC 做多 入场 60000 止损 59000" if mid == 63 else ""
                extra = {"photos": 1, "grouped": 7001}
            when = ts("2026-02-01T00:00:00") + timedelta(days=mid)
            if mid in (63, 64, 65):
                when = ts("2026-04-01T00:00:00") + timedelta(seconds=(mid - 63) * 120)
            if mid in (67, 68, 69, 70):
                root_text = next(m["text"] for m in ch.msgs if isinstance(m["text"], str) and "入场" in m["text"])
                text = "“" + root_text[:20] + "” BTC 止损上移到 60500"
            ch.add(when, text, mid=mid, **extra)
        from fixture_matrix import apply_matrix
        matrix[ch.key] = apply_matrix(ch, id_maps, ANCHORS, PEER, ts, png)
        ch.msgs.sort(key=lambda m: m["id"])
        ch.write()
        # Observation sidecar carries real receive/edit evidence separately from historical exports.
        from fixture_matrix import PINNED
        pinned_ids = {id_maps[ch.key][old] for ids in PINNED[ch.key].values() for old in ids}
        reserved = pinned_ids | set(ANCHORS.values()) | {63, 64, 65, 66, 67, 68, 69, 70, 71, 72}
        chosen = [m for m in reversed(ch.msgs) if m["id"] not in reserved and isinstance(m["text"], str) and not m.get("photo") and m["type"] == "message"][:8]
        observations = []
        for i, m in enumerate(chosen):
            for edit in range(2 if i < 2 else 1):
                seen = ts("2026-07-01T00:00:00") + timedelta(days=i, minutes=edit)
                observations.append({"peer_id": PEER[ch.key], "peer_name": ch.meta["name"], "from_id": f"channel{ch.meta['id']}", "id": m["id"], "date": int(m["date_unixtime"]),
                                     "edit_date": int(seen.timestamp()), "message": m["text"] + f" 编辑记录 {edit + 1}。", "first_seen_at": seen.isoformat(), "snapshot_at": seen.isoformat(),
                                     "cohort": "synthetic-observed-2026", "observed_sequence": i * 3 + edit})
        # H2: only the quoted fragment is observed; original source is absent.
        if ch.key == "C":
            seen = ts("2026-08-01T00:00:00")
            observations.append({"peer_id": PEER[ch.key], "id": 72, "date": int(seen.timestamp()), "message": "“不可见原帖 BTC 做多 入场 60000”", "reply_to_msg_id": 99999,
                                 "first_seen_at": seen.isoformat(), "snapshot_at": seen.isoformat(), "quote_only": True})
        observations.append(dict(observations[0]))  # exact duplicate raw observation: no extra source/version
        (ch.dir / "observations.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in observations) + "\n")
    build_delta()
    (ROOT / "SPEC.json").write_text(json.dumps({"seed": 20260911, "channels": matrix}, indent=2))
    (ROOT / "ANCHORS.json").write_text(json.dumps({"peers": PEER, "anchors": ANCHORS}, ensure_ascii=False, indent=2))
    (ROOT / "README.md").write_text("Synthetic fixture, seed=20260911; three peers, each message_id=1..72. DeltaLive is an observation sidecar of AlphaSignals. No real messages or external calls.\n")
    for ch in channels:
        print(ch.meta["name"], len(ch.msgs))


if __name__ == "__main__":
    main()
