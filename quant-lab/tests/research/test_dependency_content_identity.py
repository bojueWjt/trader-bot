"""P1-OPEN-3/4：依赖内容身份读实际字节，Requires-Dist 非 extra 递归闭包。每条能力复用正向断言并配突变。"""
from __future__ import annotations

import base64
import hashlib
import importlib
import os
import sys
from pathlib import Path

import pytest

from quant_lab.research import paths as P


def _sha_field(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _write_dist(root: Path, name: str, version: str, files: dict[str, bytes],
                requires: list[str] | None = None, extra_record: list[str] | None = None):
    from importlib.metadata import PathDistribution

    dist_info_name = f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info = root / dist_info_name
    dist_info.mkdir(parents=True)
    meta = ["Metadata-Version: 2.3", f"Name: {name}", f"Version: {version}"]
    for req in requires or []:
        meta.append(f"Requires-Dist: {req}")
    (dist_info / "METADATA").write_text("\n".join(meta) + "\n", encoding="utf-8")
    rec = []
    for rel, data in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        rec.append(f"{rel},{_sha_field(data)},{len(data)}")
    rec.append(f"{dist_info_name}/RECORD,,")
    rec.extend(extra_record or [])
    (dist_info / "RECORD").write_text("\n".join(rec) + "\n", encoding="utf-8")
    return PathDistribution(dist_info)


def _install_catalog(monkeypatch, catalog: dict, roots: tuple[str, ...] | None = None):
    monkeypatch.setattr(P, "DECLARED_DEPENDENCIES", roots or tuple(catalog))
    index = {P._canonical_dist_name(k): v for k, v in catalog.items()}

    def fake(name: str):
        key = P._canonical_dist_name(name)
        if key not in index:
            raise ModuleNotFoundError(name)
        return index[key]

    monkeypatch.setattr(P, "_distribution_for", fake)
    return fake


def _hash_only(files: dict[str, bytes]) -> str:
    pairs = [(rel, hashlib.sha256(data).hexdigest()) for rel, data in files.items()]
    h = hashlib.sha256()
    for rel, digest_hex in sorted(pairs):
        h.update(rel.encode("utf-8")); h.update(b"\0")
        h.update(digest_hex.encode("ascii")); h.update(b"\0")
    return h.hexdigest()


def _old_record_text_identity(dist, dist_name: str, version: str) -> str:
    rec = dist.read_text("RECORD")
    if rec is None:
        raise P.UnsupportedArtifactState("RECORD 不可读")
    return hashlib.sha256(rec.encode()).hexdigest()


def _mutate_equal_length(path: Path) -> None:
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])


# ----- 等长改字节：RECORD/size/mtime 在变异前采样 -----
def _assert_equal_length_change_refused(tmp_path):
    target = tmp_path / "pkg/mod.py"
    record_before = next(tmp_path.glob("*.dist-info")).joinpath("RECORD").read_text(encoding="utf-8")
    st = target.stat()
    size_before, mtime_before = st.st_size, st.st_mtime_ns
    _mutate_equal_length(target)
    os.utime(target, ns=(st.st_atime_ns, mtime_before))
    st2 = target.stat()
    assert st2.st_size == size_before
    assert st2.st_mtime_ns == mtime_before
    assert next(tmp_path.glob("*.dist-info")).joinpath("RECORD").read_text(encoding="utf-8") == record_before
    raised = None
    try:
        P.dependency_manifest()
    except P.UnsupportedArtifactState as exc:
        raised = exc
    assert raised is not None
    assert "demo-pkg" in str(raised) and "pkg/mod.py" in str(raised)


def test_equal_length_byte_change_refuses_even_if_mtime_and_record_unchanged(tmp_path, monkeypatch):
    dist = _write_dist(tmp_path, "demo-pkg", "1.0", {"pkg/mod.py": b"hello-world"})
    _install_catalog(monkeypatch, {"demo-pkg": dist})
    before = P.dependency_manifest()
    assert before["demo-pkg.content"] == _hash_only({"pkg/mod.py": b"hello-world"})
    _assert_equal_length_change_refused(tmp_path)


def test_mutant_record_text_hash_misses_equal_length_byte_change(tmp_path, monkeypatch):
    dist = _write_dist(tmp_path, "demo-pkg", "1.0", {"pkg/mod.py": b"hello-world"})
    _install_catalog(monkeypatch, {"demo-pkg": dist})
    P.dependency_manifest()
    monkeypatch.setattr(P, "_content_identity", _old_record_text_identity)
    with pytest.raises(AssertionError):
        _assert_equal_length_change_refused(tmp_path)


def _assert_missing_file_refused(tmp_path):
    (tmp_path / "pkg/mod.py").unlink()
    raised = None
    try:
        P.dependency_manifest()
    except P.UnsupportedArtifactState as exc:
        raised = exc
    assert raised is not None
    assert "demo-pkg" in str(raised) and "pkg/mod.py" in str(raised) and "缺失" in str(raised)


def test_missing_file_is_named_refusal(tmp_path, monkeypatch):
    dist = _write_dist(tmp_path, "demo-pkg", "1.0", {"pkg/mod.py": b"hello-world"})
    _install_catalog(monkeypatch, {"demo-pkg": dist})
    _assert_missing_file_refused(tmp_path)


def test_mutant_record_text_hash_misses_missing_file(tmp_path, monkeypatch):
    dist = _write_dist(tmp_path, "demo-pkg", "1.0", {"pkg/mod.py": b"hello-world"})
    _install_catalog(monkeypatch, {"demo-pkg": dist})
    monkeypatch.setattr(P, "_content_identity", _old_record_text_identity)
    with pytest.raises(AssertionError):
        _assert_missing_file_refused(tmp_path)


def _assert_unreadable_file_refused(tmp_path):
    target = tmp_path / "pkg/mod.py"
    target.unlink()
    target.mkdir()
    raised = None
    try:
        P.dependency_manifest()
    except P.UnsupportedArtifactState as exc:
        raised = exc
    assert raised is not None
    assert "demo-pkg" in str(raised) and "pkg/mod.py" in str(raised) and "不可读" in str(raised)


def test_unreadable_file_is_named_refusal(tmp_path, monkeypatch):
    dist = _write_dist(tmp_path, "demo-pkg", "1.0", {"pkg/mod.py": b"hello-world"})
    _install_catalog(monkeypatch, {"demo-pkg": dist})
    _assert_unreadable_file_refused(tmp_path)


def test_mutant_record_text_hash_misses_unreadable_file(tmp_path, monkeypatch):
    dist = _write_dist(tmp_path, "demo-pkg", "1.0", {"pkg/mod.py": b"hello-world"})
    _install_catalog(monkeypatch, {"demo-pkg": dist})
    monkeypatch.setattr(P, "_content_identity", _old_record_text_identity)
    with pytest.raises(AssertionError):
        _assert_unreadable_file_refused(tmp_path)


def _assert_record_read_text_exception():
    raised = None
    try:
        P.dependency_manifest()
    except P.UnsupportedArtifactState as exc:
        raised = exc
    assert raised is not None
    assert "RECORD 读取失败" in str(raised)


def test_record_read_text_exception_is_named_refusal(monkeypatch):
    class _Boom:
        version = "1.0"

        def read_text(self, name):
            raise OSError("boom")

    monkeypatch.setattr(P, "DECLARED_DEPENDENCIES", ("boom-pkg",))
    monkeypatch.setattr(P, "_distribution_for", lambda name: _Boom())
    _assert_record_read_text_exception()


def test_mutant_swallowing_record_read_text_exception(monkeypatch):
    class _Boom:
        version = "1.0"

        def read_text(self, name):
            raise OSError("boom")

    monkeypatch.setattr(P, "DECLARED_DEPENDENCIES", ("boom-pkg",))
    monkeypatch.setattr(P, "_distribution_for", lambda name: _Boom())
    monkeypatch.setattr(P, "_read_record_text", lambda dist, name, version: "")
    with pytest.raises(AssertionError):
        _assert_record_read_text_exception()


def _manifest_or_fail():
    try:
        return P.dependency_manifest()
    except P.UnsupportedArtifactState as exc:
        assert False, str(exc)


def _assert_unhashed_do_not_participate():
    m = _manifest_or_fail()
    assert m["demo-pkg.content"] == _hash_only({"pkg/mod.py": b"hello-world"})


def test_unhashed_empty_digest_entries_are_skipped(tmp_path, monkeypatch):
    dist = _write_dist(
        tmp_path, "demo-pkg", "1.0", {"pkg/mod.py": b"hello-world"},
        extra_record=["pkg/__pycache__/mod.cpython-312.pyc,,", "pkg/ghost.bin,,"],
    )
    _install_catalog(monkeypatch, {"demo-pkg": dist})
    _assert_unhashed_do_not_participate()


def test_mutant_unhashed_empty_digest_not_skipped(tmp_path, monkeypatch):
    dist = _write_dist(
        tmp_path, "demo-pkg", "1.0", {"pkg/mod.py": b"hello-world"},
        extra_record=["pkg/ghost.bin,,"],
    )
    _install_catalog(monkeypatch, {"demo-pkg": dist})

    real = P._content_identity

    def refuse_empty(dist, dist_name, version):
        import csv, io
        record = P._read_record_text(dist, dist_name, version)
        for row in csv.reader(io.StringIO(record)):
            if not row:
                continue
            field = (row[1] if len(row) > 1 else "").strip()
            if not field:
                raise P.UnsupportedArtifactState(f"无 sha256：{row[0]}")
        return real(dist, dist_name, version)

    monkeypatch.setattr(P, "_content_identity", refuse_empty)
    with pytest.raises(AssertionError):
        _assert_unhashed_do_not_participate()


def _assert_no_sha256_jobs_refused():
    raised = None
    try:
        P.dependency_manifest()
    except P.UnsupportedArtifactState as exc:
        raised = exc
    assert raised is not None
    assert "没有可校验的 sha256" in str(raised)


def test_no_verifiable_sha256_jobs_is_named_refusal(tmp_path, monkeypatch):
    dist = _write_dist(
        tmp_path, "demo-pkg", "1.0", {},
        extra_record=["pkg/__pycache__/mod.cpython-312.pyc,,"],
    )
    _install_catalog(monkeypatch, {"demo-pkg": dist})
    _assert_no_sha256_jobs_refused()


def test_mutant_empty_aggregate_accepts_no_sha256_jobs(tmp_path, monkeypatch):
    dist = _write_dist(tmp_path, "demo-pkg", "1.0", {})
    _install_catalog(monkeypatch, {"demo-pkg": dist})
    monkeypatch.setattr(P, "_content_identity", lambda dist, name, version: hashlib.sha256().hexdigest())
    with pytest.raises(AssertionError):
        _assert_no_sha256_jobs_refused()


def _assert_non_sha256_algorithm_refused():
    raised = None
    try:
        P.dependency_manifest()
    except P.UnsupportedArtifactState as exc:
        raised = exc
    assert raised is not None
    assert "不是 sha256" in str(raised) and "pkg/x.bin" in str(raised)


def test_non_sha256_algorithm_is_named_refusal(tmp_path, monkeypatch):
    dist = _write_dist(
        tmp_path, "demo-pkg", "1.0", {"pkg/mod.py": b"hello-world"},
        extra_record=["pkg/x.bin,md5=abcd,4"],
    )
    _install_catalog(monkeypatch, {"demo-pkg": dist})
    _assert_non_sha256_algorithm_refused()


def test_mutant_skipping_non_sha256_algorithm(tmp_path, monkeypatch):
    dist = _write_dist(
        tmp_path, "demo-pkg", "1.0", {"pkg/mod.py": b"hello-world"},
        extra_record=["pkg/x.bin,md5=abcd,4"],
    )
    _install_catalog(monkeypatch, {"demo-pkg": dist})

    real = P._content_identity

    def skip_non_sha(dist, dist_name, version):
        import csv, io
        record = P._read_record_text(dist, dist_name, version)
        kept = []
        for row in csv.reader(io.StringIO(record)):
            if not row:
                continue
            field = (row[1] if len(row) > 1 else "").strip()
            if field.startswith("sha256="):
                kept.append(",".join(row))
        class _Shim:
            version = dist.version
            def read_text(self, name):
                return "\n".join(kept) + "\n" if name == "RECORD" else dist.read_text(name)
            def locate_file(self, rel):
                return dist.locate_file(rel)
            @property
            def metadata(self):
                return dist.metadata
        return real(_Shim(), dist_name, version)

    monkeypatch.setattr(P, "_content_identity", skip_non_sha)
    with pytest.raises(AssertionError):
        _assert_non_sha256_algorithm_refused()


def _assert_hashed_pyc_mismatch_refused(tmp_path):
    target = tmp_path / "pkg/__pycache__/mod.cpython-312.pyc"
    record_before = next(tmp_path.glob("*.dist-info")).joinpath("RECORD").read_text(encoding="utf-8")
    st = target.stat()
    size_before, mtime_before = st.st_size, st.st_mtime_ns
    _mutate_equal_length(target)
    os.utime(target, ns=(st.st_atime_ns, mtime_before))
    st2 = target.stat()
    assert st2.st_size == size_before and st2.st_mtime_ns == mtime_before
    assert next(tmp_path.glob("*.dist-info")).joinpath("RECORD").read_text(encoding="utf-8") == record_before
    raised = None
    try:
        P.dependency_manifest()
    except P.UnsupportedArtifactState as exc:
        raised = exc
    assert raised is not None
    assert "mod.cpython-312.pyc" in str(raised)


def test_hashed_pyc_is_still_verified(tmp_path, monkeypatch):
    files = {"pkg/mod.py": b"hello-world", "pkg/__pycache__/mod.cpython-312.pyc": b"PYC-BYTES!"}
    dist = _write_dist(tmp_path, "demo-pkg", "1.0", files, extra_record=["pkg/__pycache__/plain.pyc,,"])
    _install_catalog(monkeypatch, {"demo-pkg": dist})
    assert P.dependency_manifest()["demo-pkg.content"] == _hash_only(files)
    _assert_hashed_pyc_mismatch_refused(tmp_path)


def test_mutant_skipping_all_pyc_misses_hashed_pyc(tmp_path, monkeypatch):
    files = {"pkg/mod.py": b"hello-world", "pkg/__pycache__/mod.cpython-312.pyc": b"PYC-BYTES!"}
    dist = _write_dist(tmp_path, "demo-pkg", "1.0", files)
    _install_catalog(monkeypatch, {"demo-pkg": dist})

    def skip_pyc(dist, dist_name, version):
        import csv, io
        record = P._read_record_text(dist, dist_name, version)
        jobs = []
        for row in csv.reader(io.StringIO(record)):
            if not row:
                continue
            rel, field = row[0], (row[1] if len(row) > 1 else "").strip()
            if field.startswith("sha256=") and not rel.endswith(".pyc"):
                jobs.append(P._verify_recorded_file(dist, dist_name, rel, field))
        return P._aggregate_content(jobs)

    monkeypatch.setattr(P, "_content_identity", skip_pyc)
    with pytest.raises(AssertionError):
        _assert_hashed_pyc_mismatch_refused(tmp_path)


# ----- 递归三层 -----
def _three_level_catalog(tmp_path):
    grand = _write_dist(tmp_path / "grand", "grand-pkg", "3.0", {"grand/x.py": b"G"})
    child = _write_dist(tmp_path / "child", "child-pkg", "2.0", {"child/x.py": b"C"}, requires=["grand-pkg"])
    root = _write_dist(tmp_path / "root", "root-pkg", "1.0", {"root/x.py": b"R"}, requires=["child-pkg"])
    return {"root-pkg": root, "child-pkg": child, "grand-pkg": grand}


def _assert_three_level_closure(m):
    for name in ("root-pkg", "child-pkg", "grand-pkg"):
        assert name in m and len(m[f"{name}.content"]) == 64, name


def test_recursive_requires_dist_includes_grandchild(tmp_path, monkeypatch):
    catalog = _three_level_catalog(tmp_path)
    monkeypatch.setattr(P, "DECLARED_DEPENDENCIES", ("root-pkg",))
    monkeypatch.setattr(
        P, "_distribution_for",
        lambda name, c=catalog: c[P._canonical_dist_name(name)],
    )
    _assert_three_level_closure(_manifest_or_fail())


def test_mutant_one_level_recursion_misses_grandchild(tmp_path, monkeypatch):
    catalog = _three_level_catalog(tmp_path)
    monkeypatch.setattr(P, "DECLARED_DEPENDENCIES", ("root-pkg",))
    monkeypatch.setattr(
        P, "_distribution_for",
        lambda name, c=catalog: c[P._canonical_dist_name(name)],
    )
    real_eff = P._effective_requires

    def one_level(dist, dist_name):
        if P._canonical_dist_name(dist_name) != "root-pkg":
            return []
        return real_eff(dist, dist_name)

    monkeypatch.setattr(P, "_effective_requires", one_level)
    with pytest.raises(AssertionError):
        _assert_three_level_closure(_manifest_or_fail())


def _assert_missing_non_extra():
    raised = None
    try:
        P.dependency_manifest()
    except P.UnsupportedArtifactState as exc:
        raised = exc
    assert raised is not None
    assert "root-pkg" in str(raised) and "missing-nonextra" in str(raised) and "未安装" in str(raised)


def test_missing_non_extra_requires_named_refusal_with_parent(tmp_path, monkeypatch):
    root = _write_dist(tmp_path, "root-pkg", "1.0", {"root/x.py": b"root-bytes"}, requires=["missing-nonextra"])
    _install_catalog(monkeypatch, {"root-pkg": root})
    _assert_missing_non_extra()


def test_mutant_skipping_missing_non_extra(tmp_path, monkeypatch):
    root = _write_dist(tmp_path, "root-pkg", "1.0", {"root/x.py": b"root-bytes"}, requires=["missing-nonextra"])
    _install_catalog(monkeypatch, {"root-pkg": root})
    monkeypatch.setattr(P, "_effective_requires", lambda dist, dist_name: [])
    with pytest.raises(AssertionError):
        _assert_missing_non_extra()


def _assert_extra_marker_excluded():
    m = _manifest_or_fail()
    assert "root-pkg" in m and "missing-gpu" not in m


def test_extra_marker_dependency_is_excluded(tmp_path, monkeypatch):
    root = _write_dist(
        tmp_path, "root-pkg", "1.0", {"root/x.py": b"root-bytes"},
        requires=['missing-gpu; extra == "gpu"'],
    )
    _install_catalog(monkeypatch, {"root-pkg": root})
    _assert_extra_marker_excluded()


def test_mutant_ignoring_extra_marker(tmp_path, monkeypatch):
    from packaging.requirements import Requirement
    root = _write_dist(
        tmp_path, "root-pkg", "1.0", {"root/x.py": b"root-bytes"},
        requires=['missing-gpu; extra == "gpu"'],
    )
    _install_catalog(monkeypatch, {"root-pkg": root})
    monkeypatch.setattr(
        P, "_effective_requires",
        lambda dist, dist_name: [Requirement(r).name for r in dist.metadata.get_all("Requires-Dist") or []],
    )
    with pytest.raises(AssertionError):
        _assert_extra_marker_excluded()


def _false_platform_marker() -> str:
    return 'sys_platform == "win32"' if sys.platform != "win32" else 'sys_platform == "darwin"'


def _assert_env_marker_excluded():
    m = _manifest_or_fail()
    assert "root-pkg" in m and "missing-otheros" not in m


def test_environment_marker_not_matching_is_excluded(tmp_path, monkeypatch):
    root = _write_dist(
        tmp_path, "root-pkg", "1.0", {"root/x.py": b"root-bytes"},
        requires=[f"missing-otheros; {_false_platform_marker()}"],
    )
    _install_catalog(monkeypatch, {"root-pkg": root})
    _assert_env_marker_excluded()


def test_mutant_ignoring_environment_marker(tmp_path, monkeypatch):
    from packaging.requirements import Requirement
    root = _write_dist(
        tmp_path, "root-pkg", "1.0", {"root/x.py": b"root-bytes"},
        requires=[f"missing-otheros; {_false_platform_marker()}"],
    )
    _install_catalog(monkeypatch, {"root-pkg": root})
    monkeypatch.setattr(
        P, "_effective_requires",
        lambda dist, dist_name: [Requirement(r).name for r in dist.metadata.get_all("Requires-Dist") or []],
    )
    with pytest.raises(AssertionError):
        _assert_env_marker_excluded()


def _assert_sorted_content(files: dict[str, bytes]):
    m = _manifest_or_fail()
    assert m["demo-pkg.content"] == _hash_only(files)


def test_content_hash_uses_sorted_path_digest_pairs(tmp_path, monkeypatch):
    files = {"pkg/b.py": b"bbbb", "pkg/a.py": b"aaaa"}
    dist = _write_dist(tmp_path, "demo-pkg", "1.0", files)
    _install_catalog(monkeypatch, {"demo-pkg": dist})
    _assert_sorted_content(files)


def test_mutant_unsorted_aggregation(tmp_path, monkeypatch):
    files = {"pkg/b.py": b"bbbb", "pkg/a.py": b"aaaa"}
    dist = _write_dist(tmp_path, "demo-pkg", "1.0", files)
    _install_catalog(monkeypatch, {"demo-pkg": dist})

    def unsorted(pairs):
        h = hashlib.sha256()
        for rel, digest_hex in pairs:
            h.update(rel.encode("utf-8")); h.update(b"\0")
            h.update(digest_hex.encode("ascii")); h.update(b"\0")
        return h.hexdigest()

    monkeypatch.setattr(P, "_aggregate_content", unsorted)
    with pytest.raises(AssertionError):
        _assert_sorted_content(files)


def _assert_cycle_canonical(counter, limit=16):
    m = _manifest_or_fail()
    assert counter["n"] <= limit, counter["n"]
    names = {k for k in m if k != "python" and not k.endswith(".content")}
    assert names == {"pkg-a", "pkg-b"}
    assert "Pkg_A" not in m and "pkg_a" not in m


def test_cycle_requires_dedup_and_canonical_names(tmp_path, monkeypatch):
    a = _write_dist(tmp_path / "a", "Pkg-A", "1.0", {"a/x.py": b"A"}, requires=["pkg_b"])
    b = _write_dist(tmp_path / "b", "pkg_b", "1.0", {"b/x.py": b"B"}, requires=["pkg_a"])
    catalog = {"pkg-a": a, "pkg-b": b}
    counter = {"n": 0}
    index = catalog

    def counted(name: str):
        counter["n"] += 1
        assert counter["n"] <= 16, f"lookup loop {counter['n']} {name}"
        key = P._canonical_dist_name(name)
        if key not in index:
            raise ModuleNotFoundError(name)
        return index[key]

    monkeypatch.setattr(P, "DECLARED_DEPENDENCIES", ("Pkg_A",))
    monkeypatch.setattr(P, "_distribution_for", counted)
    _assert_cycle_canonical(counter)


def test_mutant_no_dedup_cycles(tmp_path, monkeypatch):
    a = _write_dist(tmp_path / "a", "Pkg-A", "1.0", {"a/x.py": b"A"}, requires=["pkg_b"])
    b = _write_dist(tmp_path / "b", "pkg_b", "1.0", {"b/x.py": b"B"}, requires=["pkg_a"])
    catalog = {"pkg-a": a, "pkg-b": b}
    counter = {"n": 0}

    def counted(name: str):
        counter["n"] += 1
        if counter["n"] > 16:
            raise RuntimeError(f"lookup overflow {counter['n']}")
        return catalog[P._canonical_dist_name(name)]

    monkeypatch.setattr(P, "DECLARED_DEPENDENCIES", ("Pkg_A",))
    monkeypatch.setattr(P, "_distribution_for", counted)

    def no_dedup_manifest():
        out = {"python": sys.version.split()[0]}
        queue = list(P.DECLARED_DEPENDENCIES)
        i = 0
        while i < len(queue):
            name = queue[i]
            i += 1
            dist = P._distribution_for(name)
            key = name
            out[key] = dist.version
            out[f"{key}.content"] = P._content_identity(dist, key, dist.version)
            queue.extend(P._effective_requires(dist, key))
        return out

    monkeypatch.setattr(P, "dependency_manifest", no_dedup_manifest)
    with pytest.raises((AssertionError, RuntimeError)):
        _assert_cycle_canonical(counter)


# ----- 原生扩展覆盖：同一正向断言给真清单和六根 mutant -----
_SITE_RECORD_INDEX: dict[str, dict[str, str]] = {}


def _record_index_for_site(site: Path) -> dict[str, str]:
    import csv
    key = str(site)
    cached = _SITE_RECORD_INDEX.get(key)
    if cached is not None:
        return cached
    idx: dict[str, str] = {}
    for di in site.glob("*.dist-info"):
        record, meta = di / "RECORD", di / "METADATA"
        if not record.is_file() or not meta.is_file():
            continue
        name = None
        for line in meta.read_text(encoding="utf-8").splitlines():
            if line.startswith("Name:"):
                name = P._canonical_dist_name(line.split(":", 1)[1].strip())
                break
        if not name:
            continue
        for row in csv.reader(record.read_text(encoding="utf-8").splitlines()):
            if row:
                idx[row[0].replace("\\", "/")] = name
    _SITE_RECORD_INDEX[key] = idx
    return idx


def _dist_name_for_file(path: Path) -> str:
    path = path.resolve()
    for parent in path.parents:
        if not any(parent.glob("*.dist-info")):
            continue
        try:
            rel = path.relative_to(parent).as_posix()
        except ValueError:
            continue
        name = _record_index_for_site(parent).get(rel)
        if name:
            return name
    raise AssertionError(f"无法从路径归属分发：{path}")


def _declared_import_names() -> list[str]:
    return [name.replace("-", "_") for name in P.DECLARED_DEPENDENCIES]


def _polars_runtime_anchor():
    import polars
    node = polars._plr
    inner = getattr(node, "plr", node)
    for cand in (inner, node):
        n = getattr(cand, "__name__", None)
        f = getattr(cand, "__file__", None)
        if n or f:
            return cand, n, f
    return node, getattr(node, "__name__", None), getattr(node, "__file__", None)


def _load_declared_import_tree():
    import numpy as np
    import polars as pl
    import pyarrow as pa
    import scipy.special

    for name in _declared_import_names():
        importlib.import_module(name)
    assert float(pl.DataFrame({"a": [1.0, 2.0, 3.0]}).select(pl.col("a").sum())[0, 0]) == 6.0
    assert float(np.dot(np.arange(4.0), np.arange(4.0))) == 14.0
    assert float(scipy.special.erf(0.0)) == 0.0
    assert pa.array([1, 2, 3]).sum().as_py() == 6
    _polars_runtime_anchor()


def _site_first_component(path: Path) -> str | None:
    for parent in path.parents:
        if parent.name in {"site-packages", "dist-packages"}:
            try:
                return path.relative_to(parent).parts[0]
            except ValueError:
                return None
    return None


def _assert_native_coverage(manifest: dict):
    _load_declared_import_tree()
    import_roots = set(_declared_import_names())
    _, runtime_name, runtime_file = _polars_runtime_anchor()
    runtime_names = {runtime_name.split(".")[0]} if runtime_name else set()
    runtime_dir = Path(runtime_file).resolve().parent.name if runtime_file else None
    files: list[Path] = []
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if not f or not (f.endswith(".so") or f.endswith(".pyd")):
            continue
        path = Path(f).resolve()
        top = name.split(".")[0]
        first = _site_first_component(path)
        if top in import_roots or top in runtime_names or first in import_roots or first == runtime_dir:
            files.append(path)
    assert files, "声明依赖导入树未加载任何 .so/.pyd"
    owners = {_dist_name_for_file(p) for p in files}
    for dist_name in owners:
        assert dist_name in manifest, (dist_name, sorted(manifest))
        h = manifest[f"{dist_name}.content"]
        assert len(str(h)) == 64 and all(c in "0123456789abcdef" for c in str(h)), (dist_name, h)
    if runtime_file and (runtime_file.endswith(".so") or runtime_file.endswith(".pyd")):
        runtime_owner = _dist_name_for_file(Path(runtime_file))
        assert runtime_owner in owners
        assert runtime_owner in manifest
        assert len(str(manifest[f"{runtime_owner}.content"])) == 64


def test_loaded_native_extensions_are_in_manifest():
    _assert_native_coverage(P.dependency_manifest())


def test_mutant_six_root_manifest_fails_native_coverage(monkeypatch):
    monkeypatch.setattr(P, "_effective_requires", lambda dist, dist_name: [])
    mutant = P.dependency_manifest()
    with pytest.raises(AssertionError):
        _assert_native_coverage(mutant)
