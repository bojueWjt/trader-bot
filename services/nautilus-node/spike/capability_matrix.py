#!/usr/bin/env python3
from __future__ import annotations

import importlib
import inspect
import pkgutil
import platform
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any


REPORT_START = "<!-- CAPABILITY_MATRIX_START -->"
REPORT_END = "<!-- CAPABILITY_MATRIX_END -->"
REPO_ROOT = Path(__file__).resolve().parents[3]
REPORT_PATH = REPO_ROOT / "docs/handoff/window-b/NAUTILUS_COMPATIBILITY_REPORT.md"


@dataclass(frozen=True)
class Probe:
    name: str
    status: str
    observed: str
    source: str
    notes: str


def safe_import(module_name: str) -> tuple[ModuleType | None, str | None]:
    try:
        return importlib.import_module(module_name), None
    except Exception as exc:  # pragma: no cover - target-host diagnostic
        return None, f"{type(exc).__name__}: {exc}"


def enum_names(module_name: str, enum_name: str) -> tuple[list[str], str]:
    module, error = safe_import(module_name)
    if module is None:
        return [], error or "import failed"
    enum_cls = getattr(module, enum_name, None)
    if enum_cls is None:
        return [], f"{module_name}.{enum_name} missing"
    try:
        return [member.name for member in enum_cls], f"{module_name}.{enum_name}"
    except Exception as exc:  # pragma: no cover - target-host diagnostic
        return [], f"{type(exc).__name__}: {exc}"


def compact(items: Iterable[str], limit: int = 18) -> str:
    values = [str(item) for item in items if str(item)]
    if not values:
        return "none observed"
    if len(values) <= limit:
        return ", ".join(values)
    shown = ", ".join(values[:limit])
    return f"{shown}, ... (+{len(values) - limit} more)"


def signature_text(obj: Any) -> str:
    try:
        return str(inspect.signature(obj))
    except Exception:
        return ""


def field_names(obj: Any) -> list[str]:
    names: set[str] = set()
    for attr_name in ("model_fields", "__fields__", "__annotations__"):
        fields = getattr(obj, attr_name, None)
        if isinstance(fields, dict):
            names.update(str(name) for name in fields)
    try:
        names.update(str(name) for name in vars(obj))
    except Exception:
        pass
    text = signature_text(obj)
    if text:
        cleaned = (
            text.replace("(", " ")
            .replace(")", " ")
            .replace(",", " ")
            .replace(":", " ")
            .replace("=", " ")
        )
        for token in cleaned.split():
            if token.isidentifier():
                names.add(token)
    return sorted(names)


def load_binance_modules() -> tuple[list[ModuleType], list[str]]:
    root, error = safe_import("nautilus_trader.adapters.binance")
    if root is None:
        return [], [f"nautilus_trader.adapters.binance: {error}"]

    modules = [root]
    errors: list[str] = []
    package_path = getattr(root, "__path__", None)
    if package_path is None:
        return modules, errors

    for module_info in pkgutil.walk_packages(package_path, prefix=f"{root.__name__}."):
        module, module_error = safe_import(module_info.name)
        if module is None:
            errors.append(f"{module_info.name}: {module_error}")
        else:
            modules.append(module)
    return modules, errors


def classes_from(modules: Iterable[ModuleType]) -> list[type[Any]]:
    classes: list[type[Any]] = []
    for module in modules:
        for _, value in inspect.getmembers(module, inspect.isclass):
            if getattr(value, "__module__", "").startswith("nautilus_trader.adapters.binance"):
                classes.append(value)
    return sorted(set(classes), key=lambda cls: f"{cls.__module__}.{cls.__name__}")


def search_symbols(classes: Iterable[type[Any]], keywords: Iterable[str]) -> list[str]:
    needles = [keyword.lower() for keyword in keywords]
    matches: list[str] = []
    for cls in classes:
        haystack_parts = [cls.__name__, signature_text(cls), " ".join(field_names(cls))]
        try:
            haystack_parts.append(str(inspect.getdoc(cls) or ""))
        except Exception:
            pass
        haystack = " ".join(haystack_parts).lower()
        if any(needle in haystack for needle in needles):
            matches.append(f"{cls.__module__}.{cls.__name__}")
    return matches


def probe_config_classes(classes: Iterable[type[Any]]) -> tuple[list[str], list[str], list[str]]:
    config_lines: list[str] = []
    reconciliation_fields: list[str] = []
    testnet_fields: list[str] = []
    for cls in classes:
        if "Config" not in cls.__name__:
            continue
        names = field_names(cls)
        config_lines.append(f"{cls.__module__}.{cls.__name__}: {compact(names, 24)}")
        for name in names:
            lowered = name.lower()
            if any(part in lowered for part in ("reconcil", "cache", "timeout", "snapshot", "sync")):
                reconciliation_fields.append(f"{cls.__name__}.{name}")
            if any(part in lowered for part in ("testnet", "sandbox", "account_type")):
                testnet_fields.append(f"{cls.__name__}.{name}")
    return config_lines, sorted(set(reconciliation_fields)), sorted(set(testnet_fields))


def build_probes() -> tuple[list[Probe], list[str]]:
    modules, module_errors = load_binance_modules()
    classes = classes_from(modules)
    config_lines, reconciliation_fields, testnet_fields = probe_config_classes(classes)

    order_types, order_source = enum_names("nautilus_trader.model.enums", "OrderType")
    tif_values, tif_source = enum_names("nautilus_trader.model.enums", "TimeInForce")

    conditional_names = [
        name
        for name in order_types
        if any(part in name for part in ("STOP", "TRAILING", "TOUCHED", "TRIGGER", "CONDITIONAL"))
    ]
    reduce_only_hits = search_symbols(classes, ["reduce_only", "reduce only", "ReduceOnly"])
    precision_hits = search_symbols(classes, ["precision", "price_precision", "size_precision"])
    instrument_hits = search_symbols(classes, ["InstrumentProvider", "instrument_provider"])

    probes = [
        Probe(
            "Pinned version import",
            "known" if safe_import("nautilus_trader")[0] is not None else "unknown",
            getattr(safe_import("nautilus_trader")[0], "__version__", "not importable")
            if safe_import("nautilus_trader")[0] is not None
            else "not importable",
            "nautilus_trader.__version__",
            "Must equal 1.227.0; check_version.py enforces this.",
        ),
        Probe(
            "Binance adapter modules",
            "known" if modules else "unknown",
            f"{len(modules)} modules, {len(classes)} adapter classes",
            "pkgutil.walk_packages(nautilus_trader.adapters.binance)",
            compact(module_errors, 6) if module_errors else "All importable modules were inspected.",
        ),
        Probe(
            "USDT-M futures/testnet selectors",
            "available" if testnet_fields or search_symbols(classes, ["USDM", "USDT_FUTURE", "FUTURE"]) else "unknown",
            compact(testnet_fields or search_symbols(classes, ["USDM", "USDT_FUTURE", "FUTURE"]), 12),
            "Binance config class fields and class names",
            "Confirms only symbol/field availability; actual testnet behavior is verified by testnet_smoke.py.",
        ),
        Probe(
            "Order types",
            "core-enum" if order_types else "unknown",
            compact(order_types, 30),
            order_source,
            "Core Nautilus enum exposure; Binance adapter may reject unsupported venue combinations at runtime.",
        ),
        Probe(
            "Conditional/stop orders",
            "core-enum" if conditional_names else "unknown",
            compact(conditional_names, 24),
            order_source,
            "Requires testnet confirmation for Binance USDT-M futures acceptance and trigger mapping.",
        ),
        Probe(
            "reduce_only",
            "observed" if reduce_only_hits else "unknown",
            compact(reduce_only_hits, 12),
            "inspect Binance adapter classes/signatures/docs",
            "If not observed, use explicit position-side reconciliation plus post-fill projection until verified.",
        ),
        Probe(
            "Time in force",
            "core-enum" if tif_values else "unknown",
            compact(tif_values, 24),
            tif_source,
            "Runtime smoke must confirm exchange-specific TIF support for LIMIT/STOP order paths.",
        ),
        Probe(
            "Instrument precision",
            "observed" if precision_hits or instrument_hits else "unknown",
            compact(precision_hits + instrument_hits, 16),
            "inspect Binance adapter instrument provider/classes",
            "Strategy must round quantities/prices from loaded Instrument fields, not from hard-coded symbol rules.",
        ),
        Probe(
            "Reconciliation/cache config",
            "observed" if reconciliation_fields else "unknown",
            compact(reconciliation_fields, 24),
            "Binance config class fields",
            "Startup reconciliation remains mandatory; unknown fields must be filled from live API introspection on target.",
        ),
        Probe(
            "Config class surface",
            "observed" if config_lines else "unknown",
            compact(config_lines, 10),
            "inspect config class fields/signatures",
            "Full raw field list is intentionally compacted in this report; rerun script for exact stdout.",
        ),
    ]
    return probes, module_errors


def render_markdown(probes: list[Probe], module_errors: list[str]) -> str:
    lines = [
        "## Capability Matrix",
        "",
        f"- Generated by: `services/nautilus-node/spike/capability_matrix.py`",
        f"- Python: `{sys.version.split()[0]}`",
        f"- Platform: `{platform.platform()}`",
        "",
        "| Capability | Status | Observed | Source | Notes |",
        "|---|---|---|---|---|",
    ]
    for probe in probes:
        lines.append(
            "| "
            + " | ".join(
                cell.replace("\n", " ").replace("|", "\\|")
                for cell in (probe.name, probe.status, probe.observed, probe.source, probe.notes)
            )
            + " |"
        )

    lines.extend(["", "### Binance Adapter Import Errors", ""])
    if module_errors:
        lines.extend(f"- `{error}`" for error in module_errors)
    else:
        lines.append("- None observed during module walk.")
    lines.append("")
    return "\n".join(lines)


def replace_marked_section(path: Path, start: str, end: str, replacement: str) -> None:
    original = path.read_text(encoding="utf-8")
    if start not in original or end not in original:
        raise RuntimeError(f"Report markers missing: {start} / {end}")
    before = original.split(start, 1)[0]
    after = original.split(end, 1)[1]
    path.write_text(f"{before}{start}\n{replacement}{end}{after}", encoding="utf-8")


def main() -> int:
    probes, module_errors = build_probes()
    markdown = render_markdown(probes, module_errors)
    print(markdown)
    if REPORT_PATH.exists():
        try:
            replace_marked_section(REPORT_PATH, REPORT_START, REPORT_END, markdown)
            print(f"report_updated={REPORT_PATH}")
        except Exception as exc:  # pragma: no cover - target-host diagnostic
            print(f"report_update=FAILED: {exc!r}")
            return 1
    else:
        print(f"report_update=SKIPPED: {REPORT_PATH} not found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
