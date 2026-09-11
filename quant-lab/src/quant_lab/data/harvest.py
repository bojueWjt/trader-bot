"""PoC 1a/1b 导出接入与必测清单报告（D-08 / D-11，gated）。

只用**独立研究账号**与用户授权的导出：TDesktop JSON + 媒体归档，Telethon 次号对照拉取。
铁律：不读 services/telegram-watcher 的 session、不连生产库、零生产凭据。
报告字段：消息数 / 信号率抽样 / 编辑比例 / 图片比例 / 回复比例 / 跨年分布。
"""

from __future__ import annotations

STATUS = "skeleton"  # D-01 骨架；实现见对应任务


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="quant_lab.data.harvest", description=__doc__)
    p.add_argument("--report", required=True)
    p.add_argument("--export-dir")
    p.add_argument("--allow-network", action="store_true", help="真实拉取闸门，默认关闭")
    a = p.parse_args(argv)
    raise NotImplementedError(f"harvest 待实现（PoC 1a/1b）；args={vars(a)}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
