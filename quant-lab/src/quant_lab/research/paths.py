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


#: 进入制品身份的依赖清单。**声明式**：只认这张表上的六根，新增根依赖必须先改这里。
#: 内容身份还递归纳入每个根当前环境、非 extra 的 Requires-Dist（canonicalize 去重）。
#: pip freeze 只作附件不作证据（它报告已安装内容，不生成锁定或求解结果）。
DECLARED_DEPENDENCIES = ("polars", "numpy", "polars-ta", "arch", "scipy", "pyarrow")


def _distribution_for(name: str):
    """取已安装分发。单独抽出是为了让「RECORD 不可读」这条路径可被测试直接驱动。"""
    import importlib.metadata as _md
    return _md.distribution(name)


def _canonical_dist_name(name: str) -> str:
    from packaging.utils import canonicalize_name
    return str(canonicalize_name(name))


def _posix_rel(rel: str) -> str:
    return rel.replace("\\", "/")


def _read_record_text(dist, dist_name: str, version: str) -> str:
    try:
        record = dist.read_text("RECORD")
    except Exception as exc:
        raise UnsupportedArtifactState(
            f"声明依赖 {dist_name}=={version} 的 RECORD 读取失败"
            f"（{type(exc).__name__}），无法确定内容身份"
        ) from exc
    if record is None:
        raise UnsupportedArtifactState(
            f"声明依赖 {dist_name}=={version} 的 RECORD 不可读，无法确定内容身份——"
            f"版本号相同不代表内容相同（换源/重打包/直改 site-packages/跨平台轮子）")
    return record


def _sha256_of_located(located, dist_name: str, rel: str):
    """流式读实际字节求 sha256。每次打开文件，不用 mtime/size 缓存，不整文件 read_bytes。"""
    import hashlib
    try:
        open_fn = getattr(located, "open", None)
        fh = open_fn("rb") if callable(open_fn) else Path(located).open("rb")
        with fh:
            return hashlib.file_digest(fh, "sha256")
    except FileNotFoundError as exc:
        raise UnsupportedArtifactState(
            f"声明依赖 {dist_name} 的文件缺失，无法确定内容身份：{rel} ({located})"
        ) from exc
    except OSError as exc:
        raise UnsupportedArtifactState(
            f"声明依赖 {dist_name} 的文件不可读，无法确定内容身份：{rel} ({located})"
        ) from exc


def _verify_recorded_file(dist, dist_name: str, rel: str, recorded: str) -> tuple[str, str]:
    import base64
    try:
        located = dist.locate_file(rel)
    except Exception as exc:
        raise UnsupportedArtifactState(
            f"声明依赖 {dist_name} 的文件不可读，无法确定内容身份：{rel}"
        ) from exc
    digest = _sha256_of_located(located, dist_name, rel)
    actual_field = "sha256=" + base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
    if actual_field != recorded:
        raise UnsupportedArtifactState(
            f"声明依赖 {dist_name} 的文件内容与 RECORD 不符：{rel} ({located})"
        )
    return _posix_rel(rel), digest.hexdigest()


def _aggregate_content(pairs: list[tuple[str, str]]) -> str:
    """对排序后的 (相对路径, 实际摘要 hex) 以 path+NUL+digest+NUL 聚合 sha256。"""
    import hashlib
    h = hashlib.sha256()
    for rel, digest_hex in sorted(pairs):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(digest_hex.encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def _content_identity(dist, dist_name: str, version: str) -> str:
    """从 RECORD 逐条校验并按**实际字节**聚合内容身份。

    空 digest 条目一律跳过，不参与身份。带非 sha256 算法的条目具名拒绝。没有任何
    sha256 条目则拒绝（防空 identity）。每个 sha256 路径流式重读字节，与 wheel
    `sha256=` + urlsafe base64（无 padding）严格匹配；缺失 / 不可读 / 不符均拒绝。
    """
    import csv
    import io
    record = _read_record_text(dist, dist_name, version)
    jobs: list[tuple[str, str]] = []
    for row in csv.reader(io.StringIO(record)):
        if not row or not row[0]:
            continue
        rel = row[0]
        digest_field = (row[1] if len(row) > 1 else "").strip()
        if not digest_field:
            continue
        if not digest_field.startswith("sha256="):
            raise UnsupportedArtifactState(
                f"声明依赖 {dist_name} 的 RECORD 条目不是 sha256，无法确定内容身份："
                f"{rel} ({digest_field})"
            )
        jobs.append((rel, digest_field))
    if not jobs:
        raise UnsupportedArtifactState(
            f"声明依赖 {dist_name}=={version} 的 RECORD 没有可校验的 sha256 条目，"
            f"无法确定内容身份"
        )

    def _one(job: tuple[str, str]) -> tuple[str, str]:
        rel, recorded = job
        return _verify_recorded_file(dist, dist_name, rel, recorded)

    parallel_after, workers = 32, 4
    if len(jobs) >= parallel_after:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pairs = list(pool.map(_one, jobs))
    else:
        pairs = [_one(job) for job in jobs]
    return _aggregate_content(pairs)


def _effective_requires(dist, dist_name: str) -> list[str]:
    """当前环境、非 extra 的 Requires-Dist 名称。

    `marker.evaluate({"extra": ""})`：只选择对**当前解释器环境**成立、且不是 extra
    条件的需求。`extra == "gpu"` 这类可选依赖不进入制品身份。
    `sys_platform` / `python_version` 等环境 marker 不生效则排除——它们在本机不会被
    导入，不构成当前制品的执行闭包；换平台会得到另一份制品，这是正确行为。
    """
    from packaging.requirements import Requirement
    try:
        raws = list(dist.metadata.get_all("Requires-Dist") or [])
    except Exception as exc:
        raise UnsupportedArtifactState(
            f"声明依赖 {dist_name} 的 METADATA/Requires-Dist 不可读：{type(exc).__name__}"
        ) from exc
    names: list[str] = []
    for raw in raws:
        try:
            req = Requirement(raw)
        except Exception as exc:
            raise UnsupportedArtifactState(
                f"声明依赖 {dist_name} 的 Requires-Dist 无法解析 {raw!r}：{type(exc).__name__}"
            ) from exc
        if req.marker is not None:
            try:
                if not req.marker.evaluate({"extra": ""}):
                    continue
            except Exception as exc:
                raise UnsupportedArtifactState(
                    f"声明依赖 {dist_name} 的 Requires-Dist marker 无法求值 {raw!r}："
                    f"{type(exc).__name__}"
                ) from exc
        names.append(req.name)
    return names


def dependency_manifest() -> dict:
    """声明依赖闭包的**版本 + 内容哈希** + 解释器版本。任一不可得即具名拒绝，不静默跳过。

    只记版本号不够（G0 R-10 裁定 §10）：版本串相同而包内容不同，可经换安装源、重打包 wheel、
    直改 site-packages、跨平台轮子四条路径发生，**四条都不需要向 worker 注入任何代码**，
    因此属事故类，必须挡住。

    六根见 `DECLARED_DEPENDENCIES`。对每个根分发递归读取 Requires-Dist，用 packaging
    `Requirement` 与 `marker.evaluate({"extra": ""})` 留下当前环境非 extra 依赖，
    `canonicalize_name` 且循环去重。生效但未安装的依赖具名拒绝，含父依赖与缺失名。

    每个分发的 `.content` 是 RECORD 所列文件**实际字节**的聚合摘要（见 `_content_identity`），
    不是 RECORD 文本自身的哈希——直改 site-packages 而 RECORD 不变时必须拒绝。
    无哈希条目（RECORD 自身、安装生成 pyc 等）不参与：RECORD 自引用不能自哈希、pyc 安装生成；
    陈旧 pyc 执行侧另行处理，不在本函数范围。带 sha256 的条目（含 pyc）仍校验实际字节。
    返回 `{"python": 版本, "<dist>": 版本, "<dist>.content": 64hex, ...}`。
    """
    import sys
    out = {"python": sys.version.split()[0]}
    queue = list(DECLARED_DEPENDENCIES)
    seen: set[str] = set()
    parent_of: dict[str, str] = {}
    for name in queue:
        key = _canonical_dist_name(name)
        if key in seen:
            continue
        seen.add(key)
        try:
            dist = _distribution_for(name)
            version = dist.version
        except Exception as exc:
            parent = parent_of.get(key)
            if parent:
                raise UnsupportedArtifactState(
                    f"依赖 {parent} 需要 {key}，但 {key} 未安装：{type(exc).__name__}"
                ) from exc
            raise UnsupportedArtifactState(
                f"声明依赖 {name} 不可得：{type(exc).__name__}"
            ) from exc
        out[key] = version
        out[f"{key}.content"] = _content_identity(dist, key, version)
        for child in _effective_requires(dist, key):
            child_key = _canonical_dist_name(child)
            if child_key not in seen:
                parent_of.setdefault(child_key, key)
                queue.append(child)
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
