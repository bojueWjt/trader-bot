#!/usr/bin/env python3
"""Static gate for the retired Hermes semantic importer path."""

from __future__ import annotations

import fnmatch
import re
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = (
    "services",
    "packages",
    "bridge/services",
    "bridge/apps/api",
)

NOQA_RE = re.compile(r"(?:#|//)\s*noqa:\s*semantic-regex\s*--\s*(?P<reason>.+)")


def token(*parts: str) -> str:
    return "".join(parts)


LITERAL_VIOLATIONS = (
    (token("--approve", "-parsed"), "retired auto-approve flag"),
    (token("--refresh", "-window"), "retired refresh-window flag"),
)

CALL_VIOLATIONS = (
    (
        re.compile(r"\bimportSignalToFreqtrade\s*\("),
        re.compile(r"\bfunction\s+importSignalToFreqtrade\s*\("),
        "production call to importSignalToFreqtrade()",
    ),
    (
        re.compile(r"\bforwardToTrader\s*\("),
        re.compile(r"\bfunction\s+forwardToTrader\s*\("),
        "production call to forwardToTrader()",
    ),
)

SEMANTIC_IMPORT_VIOLATIONS = (
    (
        re.compile(r"\brequire\s*\(\s*['\"][^'\"]*signal-importer['\"]\s*\)"),
        "production import of signal-importer",
    ),
    (
        re.compile(r"\bfrom\s+['\"][^'\"]*signal-importer['\"]"),
        "production import of signal-importer",
    ),
    (
        re.compile(r"\bimport\s+[^;\n]*['\"][^'\"]*signal-importer['\"]"),
        "production import of signal-importer",
    ),
    (
        re.compile(r"\bfrom\s+freqtrade\.signal_strategy\.importer\b"),
        "production import of freqtrade signal importer",
    ),
    (
        re.compile(r"\bimport\s+freqtrade\.signal_strategy\.importer\b"),
        "production import of freqtrade signal importer",
    ),
    (
        re.compile(r"(?:^|[;&|]\s*)[\w./-]*python[0-9.]*\s+-m\s+freqtrade\.signal_strategy\.importer\b"),
        "production command invokes freqtrade signal importer",
    ),
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line_no: int
    reason: str


@dataclass(frozen=True)
class Exemption:
    path: Path
    line_no: int
    reason: str


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def is_excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = rel.parts

    if "engine" in parts or "docs" in parts:
        return True
    if "__tests__" in parts or "tests" in parts or "fixtures" in parts or "node_modules" in parts:
        return True
    if any(part.startswith("test") for part in parts[:-1]):
        return True

    name = path.name
    if fnmatch.fnmatch(name, "*.test.*") or fnmatch.fnmatch(name, "*.spec.*"):
        return True

    return False


def iter_files() -> list[Path]:
    files: list[Path] = []
    for scan_dir in SCAN_DIRS:
        base = ROOT / scan_dir
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and not is_excluded(path):
                files.append(path)
    return sorted(files)


def read_text(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def line_exemption(path: Path, line_no: int, line: str) -> Exemption | None:
    match = NOQA_RE.search(line)
    if not match:
        return None
    return Exemption(path=path, line_no=line_no, reason=match.group("reason").strip())


def line_findings(path: Path, line_no: int, line: str) -> list[Finding]:
    findings: list[Finding] = []

    for literal, reason in LITERAL_VIOLATIONS:
        if literal in line:
            findings.append(Finding(path=path, line_no=line_no, reason=reason))

    for call_re, declaration_re, reason in CALL_VIOLATIONS:
        if call_re.search(line) and not declaration_re.search(line):
            findings.append(Finding(path=path, line_no=line_no, reason=reason))

    for import_re, reason in SEMANTIC_IMPORT_VIOLATIONS:
        if import_re.search(line):
            findings.append(Finding(path=path, line_no=line_no, reason=reason))

    return findings


def main() -> int:
    scanned = 0
    findings: list[Finding] = []
    exemptions: list[Exemption] = []

    for path in iter_files():
        text = read_text(path)
        if text is None:
            continue
        scanned += 1

        for line_no, line in enumerate(text.splitlines(), start=1):
            exemption = line_exemption(path, line_no, line)
            if exemption:
                exemptions.append(exemption)

            current_findings = line_findings(path, line_no, line)
            if exemption:
                continue
            findings.extend(current_findings)

    for finding in findings:
        print(f"{relative(finding.path)}:{finding.line_no}: {finding.reason}")

    print(f"Scanned {scanned} files")
    print(f"{len(findings)} violations")

    if exemptions:
        print("Exemptions:")
        for exemption in exemptions:
            print(f"- {relative(exemption.path)}:{exemption.line_no}: {exemption.reason}")
    else:
        print("Exemptions: 0")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
