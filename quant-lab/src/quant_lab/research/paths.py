"""研究层路径解析 —— 湖根目录一律经 QUANT_LAB_DATA_ROOT（feature-snapshot §7.5 / research-schema §9.1）。

不得写死相对 data/：G0 的 OR-04 冒烟会把该变量指向 tmp 目录。
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_DATA_ROOT = "QUANT_LAB_DATA_ROOT"

_PKG_ROOT = Path(__file__).resolve().parents[3]  # .../quant-lab/src -> quant-lab
REPO_ROOT = _PKG_ROOT.parent if _PKG_ROOT.name == "src" else _PKG_ROOT


def data_root() -> Path:
    override = os.environ.get(ENV_DATA_ROOT)
    return Path(override).resolve() if override else REPO_ROOT / "data"


def lockbox_dir() -> Path:
    return data_root() / "lockbox"


def ledger_path() -> Path:
    return lockbox_dir() / "ledger.parquet"


def feature_cache_dir() -> Path:
    return lockbox_dir() / "feature_cache"


def research_code_digest() -> str:
    """研究代码血缘摘要：**递归**覆盖 src/quant_lab/research/**/*.py（路径 + NUL + 字节，按路径排序）。

    唯一来源——账本的 `code_version`、报告内嵌的 `research_code_sha256` 都用它（review-G3-P1 四审 R4-L：
    原先账本只 glob 顶层 *.py，backends/ 的实现改动不改血缘，等于审计看不见后端算子的变化）。
    """
    import hashlib
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for f in sorted(root.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        h.update(str(f.relative_to(root)).encode()); h.update(b"\0"); h.update(f.read_bytes()); h.update(b"\0")
    return h.hexdigest()


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


__all__ = ["ENV_DATA_ROOT", "REPO_ROOT", "data_root", "ensure_dir", "research_code_digest", "feature_cache_dir", "ledger_path", "lockbox_dir"]
