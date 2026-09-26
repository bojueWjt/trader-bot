"""Development-time compiler for the watcher route contract."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "contracts/watcher-gateway-routes.yaml"
PYTHON = ROOT / "services/control-plane/api/generated/watcher_gateway_routes.py"
JAVASCRIPT = ROOT / "bridge/services/telegram-watcher/lib/generated/gateway-routes.js"
OUTPUTS = (PYTHON, JAVASCRIPT)
PHASES = ("P0", "P1", "P2", "P3")
TOP_KEYS = set("schema_version contract_version frozen_at amended_at source_plan paths roles identities actor_headers regex_dialect secret_fields secret_key_pattern budgets path_params query_params body_fields response_headers media_request_headers invariants never_allowed gateway_excluded routes".split())
ROW_KEYS = set("id phase identity method outer_path inner_path roles query body response budget write".split())
SECRET_FIELDS = {"api_key", "api_secret", "apiKey", "apiSecret", "api_id", "api_hash", "apiId", "apiHash", "session", "sessionString", "password", "phoneNumber", "phone", "code", "token"}
SNAPSHOT_FIELDS = {
    "envelope": ["schema_version", "revision", "content_sha256", "generated_at", "accounts", "channels", "risks"],
    "accounts": ["account_id", "kind", "parent_account_id", "execution_account_id", "enabled", "risk_capital_addon", "default_risk"],
    "channels": ["channel_id", "target_account_id"],
    "risks": ["symbol", "risk_ratio"],
}
EXPECTED = {
    ("P0", "GET", "/v1/watcher/status"),
    *(("P1", m, "/v1/watcher/" + p) for m, p in (
        ("GET", "trading/messages"), ("GET", "trading/briefings"),
        ("GET", "media/{filename}"), ("HEAD", "media/{filename}"),
        ("POST", "disconnect"), ("POST", "reconnect"),
        ("GET", "trading/orders"), ("GET", "trading/orders/active"))),
    *(("P2", m, "/v1/watcher/" + p) for m, p in (
        ("GET", "dialogs"), ("POST", "groups"), ("GET", "trading/accounts"),
        ("PUT", "trading/accounts/{account_id}"), ("DELETE", "trading/accounts/{account_id}"),
        ("GET", "trading/channels"), ("POST", "trading/channels"),
        ("DELETE", "trading/channels/{channel_id}"), ("GET", "trading/risks"),
        ("POST", "trading/risks"), ("DELETE", "trading/risks/{symbol}"))),
    *(("P3", m, "/v1/watcher/" + p) for m, p in (
        ("GET", "price-alerts"), ("POST", "price-alerts"),
        ("DELETE", "price-alerts/{alert_id}"), ("GET", "price-monitor/status"))),
}


class EnvironmentError(RuntimeError):
    pass


def load_source(path: Path = SOURCE):
    try:
        import yaml
    except ImportError as exc:
        raise EnvironmentError("PyYAML missing; use system python3") from exc
    if shutil.which("node") is None:
        raise EnvironmentError("node missing")
    raw = path.read_bytes()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"S-01 YAML parse: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("S-01 top level must be object")
    return data, hashlib.sha256(raw).hexdigest()


def _check(test, number, reason):
    if not test:
        raise ValueError(f"S-{number:02d} {reason}")


def _path_matches(template, path):
    parts = []
    for part in template.split("/"):
        if part == "*":
            parts.append(".+")
        elif part.startswith("{") and part.endswith("}"):
            parts.append("[^/]+")
        else:
            parts.append(re.escape(part))
    return re.fullmatch("/".join(parts), path) is not None


def _patterns(data):
    found = []
    for item in data["path_params"].values():
        found.extend(v for k, v in item.items() if k.endswith("_pattern") and v is not None)
    for key in ("query_params", "body_fields"):
        for item in data[key].values():
            found.extend(v for k, v in item.items() if k.endswith("pattern") and v is not None)
    found.extend(v for k, v in data["actor_headers"].items() if k.endswith("_pattern"))
    return [(p, "") for p in found] + [(data["secret_key_pattern"]["pattern"], data["secret_key_pattern"]["flags"])]


def _regex_check(data):
    probes = ["", "a", "A", "0", "abc\n", "\n", "a b", "%2F", "..", "ſ", "K", "ſession", "toKen", "😀", "api_key", "API_KEY", "rapid_x", "apı_key", "apİ_key"]
    for definition in (*data["query_params"].values(), *data["body_fields"].values()):
        probes.extend(definition.get("enum", []))
        probes.extend(definition.get("gateway_enum", []))
    items = []
    for pattern, flags in _patterns(data):
        _check(isinstance(pattern, str) and isinstance(flags, str), 20, "pattern/flags type")
        _check(all(c in data["regex_dialect"]["allowed_flags"] for c in flags), 20, "flags")
        _check(not re.search(r"\(\?(?!:)", pattern), 20, "nonportable group")
        _check(not re.search(r"(?<!\\)\\[dwsbDWSB]", pattern), 20, "nonportable shorthand")
        in_class = False
        escaped = False
        for c in pattern:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == "[":
                in_class = True
            elif c == "]":
                in_class = False
            elif c == "." and not in_class:
                _check(False, 20, "wildcard dot")
        if (pattern, flags) != (data["secret_key_pattern"]["pattern"], data["secret_key_pattern"]["flags"]):
            _check(pattern.startswith("^") and pattern.endswith("$"), 20, "anchor")
        compiled = re.compile(pattern, re.I if "i" in flags else 0)
        secret = pattern == data["secret_key_pattern"]["pattern"]
        py_results = [bool((not p.isascii() or compiled.search(p)) if secret else compiled.fullmatch(p)) for p in probes]
        items.append({"pattern": pattern, "flags": flags, "probes": probes, "expected": py_results, "secret": secret})
    script = "const a=JSON.parse(process.argv[1]); for(const x of a){const r=new RegExp(x.pattern,'u'+x.flags); for(let i=0;i<x.probes.length;i++){const p=x.probes[i];const m=r.exec(p);let v=x.secret?(/[^\\x00-\\x7f]/u.test(p)||!!m):!!m&&m[0]===p;if(v!==x.expected[i])process.exit(1)}}"
    run = subprocess.run(["node", "-e", script, json.dumps(items, ensure_ascii=True)], capture_output=True, text=True, check=False)
    _check(run.returncode == 0, 20, "Python/JS regex disagreement or JS compile failure")


def validate(data):
    _check(set(data) == TOP_KEYS and data["schema_version"] == "watcher-gateway-routes.v1", 1, "top level/schema")
    _check(re.fullmatch(r"WGW-[0-9]+\.[0-9]+(?:\.[0-9]+)?", data["contract_version"]), 1, "version")
    for key in ("frozen_at", "amended_at"):
        _check(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(data[key])), 1, key)
        try:
            date.fromisoformat(str(data[key]))
        except ValueError:
            _check(False, 1, key)
    rows = data["routes"]
    _check(isinstance(rows, list) and bool(rows), 1, "zero routes")
    _check(set(data["secret_key_pattern"]) == {"pattern", "flags"}, 10, "secret regex shape")
    configured_secrets = set(data["secret_fields"])
    _check(SECRET_FIELDS <= configured_secrets, 10, "secret_fields baseline")
    secret = re.compile(data["secret_key_pattern"]["pattern"], re.I if "i" in data["secret_key_pattern"]["flags"] else 0)
    ids, inner_keys, outer_keys = set(), set(), set()
    snapshots = []
    by_identity = set()
    for row in rows:
        _check(set(row) == ROW_KEYS and {"allow", "deny", "required"} <= set(row["body"]) <= {"allow", "deny", "required", "field_overrides"}, 2, str(row.get("id")))
        expected_response = {"omit", "mask", "headers", "fields"} if row["identity"] == "snapshot" else {"omit", "mask", "headers"}
        _check(set(row["response"]) == expected_response, 2, str(row["id"]))
        identity, method, path = row["identity"], row["method"], row["inner_path"]
        by_identity.add(identity)
        prefix = {"gateway": "gw", "snapshot": "snap", "browser": "br"}.get(identity)
        _check(prefix is not None, 4, str(identity))
        _check(re.fullmatch(r"(gw|snap|br)\.[a-z0-9_]+\.(get|head|post|put|delete)", row["id"]) and row["id"].startswith(prefix + ".") and row["id"].endswith("." + method.lower()) and row["id"] not in ids, 3, row["id"])
        ids.add(row["id"])
        _check(row["phase"] in PHASES and (identity == "gateway" or row["phase"] == "P0"), 5, row["id"])
        _check(method in {"GET", "HEAD", "POST", "PUT", "DELETE"} and (identity, method, path) not in inner_keys, 6, row["id"])
        inner_keys.add((identity, method, path))
        if identity == "gateway":
            outer = row["outer_path"]
            _check((method, outer) not in outer_keys, 6, row["id"])
            outer_keys.add((method, outer))
            prefix_path = data["paths"]["app_outer_prefix"]
            _check(outer.startswith(prefix_path + "/"), 7, row["id"])
            rest = outer[len(prefix_path):]
            _check(path == (rest if rest.startswith("/media/") else "/api" + rest), 7, row["id"])
            _check(re.findall(r"\{([^{}]+)\}", outer) == re.findall(r"\{([^{}]+)\}", path), 7, row["id"])
            _check(set(row["roles"]) == set(data["roles"]["all_readers"]) if method in {"GET", "HEAD"} else row["roles"] == data["roles"]["writer"], 9, row["id"])
            _check(configured_secrets <= set(row["body"]["deny"]), 10, row["id"])
            _check(not (set(row["body"]["allow"]) & configured_secrets), 10, row["id"])
            _check(not any(secret.search(k) for k in row["body"]["allow"] + row["query"]), 10, row["id"])
            _check(row["budget"] == ("media" if path.startswith("/media/") else "config"), 14, row["id"])
            _check(row["response"]["mask"] == [], 16, row["id"])
        else:
            _check(row["outer_path"] is None and row["roles"] is None, 8, row["id"])
            _check(row["budget"] == ("snapshot" if identity == "snapshot" else None), 8, row["id"])
        body = row["body"]
        _check(not set(body["allow"]) & set(body["deny"]) and set(body["required"]) <= set(body["allow"]), 10, row["id"])
        overrides = body.get("field_overrides") or {}
        _check(set(overrides) <= set(body["allow"]), 11, row["id"])
        for field in body["allow"]:
            definition = data["body_fields"].get(overrides.get(field, field))
            _check(definition is not None and (identity != "gateway" or not (definition.get("secret") or definition.get("browser_only"))), 11, row["id"])
        _check(set(row["query"]) <= set(data["query_params"]), 11, row["id"])
        for param in re.findall(r"\{([^{}]+)\}", path):
            _check(data["path_params"].get(param, {}).get("gateway_pattern" if identity == "gateway" else "browser_pattern") is not None, 11, row["id"])
        if method in {"GET", "HEAD"}:
            _check(body["allow"] == [] and row["write"] is None, 12, row["id"])
        elif row["write"] is not None:
            write = row["write"]
            _check("client_ref" in body["required"], 12, row["id"])
            _check(not write.get("conditional") or "expected_revision" in body["required"], 12, row["id"])
            _check(not write.get("bumps_revision") or write.get("conditional"), 12, row["id"])
            _check(write.get("idempotency") in {"transactional", "reentrant"}, 12, row["id"])
        else:
            _check(identity == "browser" and (path == "/api/config" or path.startswith("/api/login/")), 12, row["id"])
        if method == "HEAD":
            _check(path.startswith("/media/") and (identity, "GET", path) in inner_keys | {(r["identity"], r["method"], r["inner_path"]) for r in rows}, 13, row["id"])
        if path.startswith("/media/"):
            _check(row["response"]["headers"] == "media", 13, row["id"])
        _check(row["response"]["headers"] in data["response_headers"], 15, row["id"])
        if row["response"]["headers"] == "json_config_read":
            _check(method == "GET" and any(path.startswith(p) for p in ("/api/trading/accounts", "/api/trading/channels", "/api/trading/risks")), 15, row["id"])
        if identity == "snapshot":
            snapshots.append(row)
        if "/accounts" in path:
            if identity == "gateway":
                _check({"api_key", "api_secret"} <= set(row["response"]["omit"]), 16, row["id"])
            if identity == "browser":
                _check({"api_key", "api_secret"} <= set(row["response"]["mask"]), 16, row["id"])
    _check(by_identity == {"gateway", "snapshot", "browser"}, 4, "missing identity")
    _check(len(snapshots) == 1 and snapshots[0]["method"] == "GET" and snapshots[0]["inner_path"] == "/api/trading/config-snapshot", 17, "snapshot count/path")
    _check(snapshots[0]["response"]["fields"] == SNAPSHOT_FIELDS, 17, "snapshot fields")
    for excluded in data["never_allowed"]:
        _check(excluded["methods"] == "*", 18, "never_allowed methods")
    for row in rows:
        if row["identity"] != "gateway":
            continue
        for excluded in data["never_allowed"]:
            _check(not _path_matches(excluded["inner_path"], row["inner_path"]), 18, row["id"])
        for excluded in data["gateway_excluded"]:
            _check(not (_path_matches(excluded["inner_path"], row["inner_path"]) and (excluded["methods"] == "*" or row["method"] in excluded["methods"])), 18, row["id"])
    _check({(r["phase"], r["method"], r["outer_path"]) for r in rows if r["identity"] == "gateway"} == EXPECTED, 19, "plan §3 route coverage")
    _regex_check(data)
    return len(rows)


def payload(data, yaml_sha256, phase_max):
    if phase_max not in PHASES:
        raise ValueError("phase_max must be P0..P3")
    result = {k: v for k, v in data.items() if k not in {"invariants", "never_allowed", "gateway_excluded"}}
    result["routes"] = sorted((r for r in data["routes"] if r["identity"] != "gateway" or PHASES.index(r["phase"]) <= PHASES.index(phase_max)), key=lambda r: (r["identity"], r["inner_path"], r["method"]))
    result["_meta"] = {"contract_version": data["contract_version"], "schema_version": data["schema_version"], "yaml_sha256": yaml_sha256, "phase_max": phase_max, "generator": "scripts/contracts/gen_watcher_gateway_routes.py"}
    return result


def render(data, yaml_sha256, phase_max):
    value = payload(data, yaml_sha256, phase_max)
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    literal = json.dumps(canonical, ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    header = "GENERATED by scripts/contracts/gen_watcher_gateway_routes.py from contracts/watcher-gateway-routes.yaml — DO NOT EDIT"
    py = f'''# {header}
import json
import hashlib
import re

_TEXT = {literal}
PAYLOAD_SHA256 = "{digest}"
if hashlib.sha256(_TEXT.encode()).hexdigest() != PAYLOAD_SHA256:
    raise ValueError("watcher gateway route payload digest mismatch")
PAYLOAD = json.loads(_TEXT)
_secret = PAYLOAD["secret_key_pattern"]
SECRET_KEY_REGEX = re.compile(_secret["pattern"], re.IGNORECASE if "i" in _secret["flags"] else 0)
'''
    js = f'''// {header}
'use strict';
const crypto = require('node:crypto');
const _TEXT = {literal};
const PAYLOAD_SHA256 = '{digest}';
if (crypto.createHash('sha256').update(_TEXT, 'utf8').digest('hex') !== PAYLOAD_SHA256) {{
  throw new Error('watcher gateway route payload digest mismatch');
}}
const PAYLOAD = JSON.parse(_TEXT);
function deepFreeze(value) {{
  if (value && typeof value === 'object') {{
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }}
  return value;
}}
deepFreeze(PAYLOAD);
const SECRET_KEY_REGEX = new RegExp(PAYLOAD.secret_key_pattern.pattern, 'u' + PAYLOAD.secret_key_pattern.flags);
module.exports = {{ PAYLOAD, PAYLOAD_SHA256, SECRET_KEY_REGEX }};
'''
    return {PYTHON: py.encode(), JAVASCRIPT: js.encode()}
