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


class UnsupportedArtifactState(RuntimeError):
    """制品身份无法确定时的具名拒绝。"""


#: 进入制品身份的依赖清单。**声明式**：只认这张表上的包，新增依赖必须先改这里。
#: pip freeze 只作附件不作证据（它报告已安装内容，不生成锁定或求解结果）。
DECLARED_DEPENDENCIES = ("polars", "numpy", "polars-ta", "arch", "scipy", "pyarrow")


def _distribution_for(name: str):
    """取已安装分发。单独抽出是为了让「RECORD 不可读」这条路径可被测试直接驱动。"""
    import importlib.metadata as _md
    return _md.distribution(name)


def dependency_manifest() -> dict:
    """声明依赖的**版本 + 内容哈希** + 解释器版本。任一不可得即具名拒绝，不静默跳过。

    只记版本号不够（G0 R-10 裁定 §10）：版本串相同而包内容不同，可经换安装源、重打包 wheel、
    直改 site-packages、跨平台轮子四条路径发生，**四条都不需要向 worker 注入任何代码**，
    因此属事故类，必须挡住。内容身份直接取该分发 `RECORD` 的摘要——装包时已对每个文件算好
    sha256，不必自己遍历文件树；`RECORD` 不可读的分发**具名拒绝**，与「版本不可得即拒绝」同构。
    """
    import hashlib
    import importlib.metadata as _md
    import sys
    out = {"python": sys.version.split()[0]}
    for name in DECLARED_DEPENDENCIES:
        try:
            dist = _distribution_for(name)
            version = dist.version
        except Exception as exc:
            raise UnsupportedArtifactState(f"声明依赖 {name} 不可得：{type(exc).__name__}") from exc
        record = dist.read_text("RECORD")
        if record is None:
            raise UnsupportedArtifactState(
                f"声明依赖 {name}=={version} 的 RECORD 不可读，无法确定内容身份——"
                f"版本号相同不代表内容相同（换源/重打包/直改 site-packages/跨平台轮子）")
        # 版本与内容身份**分开成两个字段**：二者回答不同的问题，合成一串会让「版本相同」
        # 与「内容相同」再次混为一谈，而这正是本条要挡的东西。
        out[name] = version
        out[f"{name}.content"] = hashlib.sha256(record.encode()).hexdigest()
    return out


def artifact_identity() -> dict:
    """运行制品身份：**冻结源码摘要 + 声明依赖清单**，不反射任何运行时对象。

    这里刻意**不再**去推断「哪些内存状态影响行为」。G0 R-10 裁定 §2.2/§2.3：那条路线在
    「任意外部 Python callable」支持域下没有可信收敛点，改为结构性关闭——让未申报的状态
    根本过不去进程边界（全新解释器 spawn + 冻结制品 + 纯数据配置 + 逐 job 回执），
    而不是检测它们。白名单**替代**扫描，不是叠在扫描前面。
    """
    return {"source": research_code_digest(), "deps": dependency_manifest()}


def artifact_identity_digest() -> str:
    import hashlib
    import json
    return hashlib.sha256(json.dumps(artifact_identity(), sort_keys=True).encode()).hexdigest()


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


__all__ = ["ENV_DATA_ROOT", "REPO_ROOT", "data_root", "artifact_identity", "artifact_identity_digest", "dependency_manifest", "ensure_dir", "research_code_digest", "feature_cache_dir", "ledger_path", "lockbox_dir"]
