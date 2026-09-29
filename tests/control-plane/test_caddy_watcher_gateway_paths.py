"""Caddy watcher gateway path list (contracts/backend-api.md §9.14.3 / §9.14.4)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
YAML = ROOT / "contracts/watcher-gateway-routes.yaml"
CADDY = ROOT / "contracts/generated/caddy-watcher-gateway-paths.txt"
SNIPPET = ROOT / "contracts/generated/caddy-watcher-gateway.caddy"
PYTHON = ROOT / "services/control-plane/api/generated/watcher_gateway_routes.py"
JAVASCRIPT = ROOT / "bridge/services/telegram-watcher/lib/generated/gateway-routes.js"
CHECK = ROOT / "scripts/contracts/check_watcher_gateway_routes.py"
LIBRARY = ROOT / "scripts/contracts/watcher_gateway_routes_lib.py"
OUTPUTS = (PYTHON, JAVASCRIPT, CADDY, SNIPPET)

# Independent of YAML (plan §3 / backend-api.md §9.14.2 P0–P2).
EXPECTED_P2_LINES = (
    "/m/v1/watcher/dialogs ^/m/v1/watcher/dialogs$ GET",
    "/m/v1/watcher/disconnect ^/m/v1/watcher/disconnect$ POST",
    "/m/v1/watcher/groups ^/m/v1/watcher/groups$ POST",
    "/m/v1/watcher/media/{filename} ^/m/v1/watcher/media/[^/]+$ GET HEAD",
    "/m/v1/watcher/reconnect ^/m/v1/watcher/reconnect$ POST",
    "/m/v1/watcher/status ^/m/v1/watcher/status$ GET",
    "/m/v1/watcher/trading/accounts ^/m/v1/watcher/trading/accounts$ GET",
    "/m/v1/watcher/trading/accounts/{account_id} ^/m/v1/watcher/trading/accounts/[^/]+$ DELETE PUT",
    "/m/v1/watcher/trading/briefings ^/m/v1/watcher/trading/briefings$ GET",
    "/m/v1/watcher/trading/channels ^/m/v1/watcher/trading/channels$ GET POST",
    "/m/v1/watcher/trading/channels/{channel_id} ^/m/v1/watcher/trading/channels/[^/]+$ DELETE",
    "/m/v1/watcher/trading/messages ^/m/v1/watcher/trading/messages$ GET",
    "/m/v1/watcher/trading/orders ^/m/v1/watcher/trading/orders$ GET",
    "/m/v1/watcher/trading/orders/active ^/m/v1/watcher/trading/orders/active$ GET",
    "/m/v1/watcher/trading/risks ^/m/v1/watcher/trading/risks$ GET POST",
    "/m/v1/watcher/trading/risks/{symbol} ^/m/v1/watcher/trading/risks/[^/]+$ DELETE",
)
P3_PATHS = (
    "/m/v1/watcher/price-alerts",
    "/m/v1/watcher/price-alerts/{alert_id}",
    "/m/v1/watcher/price-monitor/status",
)

# Snippet format v2 (backend-api.md §9.14.3, WGW-1.0.3): written out here by
# hand, independent of scripts/contracts/watcher_gateway_routes_lib.py.
FALLBACK_REGEX = r"^(?i:/m/v1/watcher)(?:[/\n]|$)"
DIRECT_REGEX = r"^(?i:/v1/watcher)(?:[/\n%]|$)"
FALLBACK_GROUP = (
    "\t@wgw_fallback {\n"
    "\t\tpath_regexp ^(?i:/m/v1/watcher)(?:[/\\n]|$)\n"
    "\t}\n"
    "\thandle @wgw_fallback {\n"
    "\t\trespond 404\n"
    "\t}\n"
)
DIRECT_GROUP = (
    "\t@wgw_direct {\n"
    "\t\tpath_regexp ^(?i:/v1/watcher)(?:[/\\n%]|$)\n"
    "\t}\n"
    "\thandle @wgw_direct {\n"
    "\t\trespond 404\n"
    "\t}\n"
)
V2_TAIL = FALLBACK_GROUP + DIRECT_GROUP + "}\n(watcher_gateway_direct_guard) {\n" + DIRECT_GROUP + "}\n"
DIRECT_MATCH = (
    "/v1/watcher", "/v1/watcher/", "/V1/WATCHER/status", "/v1/watcher/status",
    "/v1/watcher\n", "/V1/WATCHER\n", "/v1/watcher/status\n", "/v1/watcher%",
    "/v1/watcher%x", "/v1/watcher/media/1700000000000-1.png", "/v1/watcher/status#x",
)
DIRECT_NO_MATCH = (
    "/v1/watcherx", "/v1/watcherx\n", "/v1/watcher\r", "/v1/watcher#x", "/v1/other",
    "/v1/accounts", "/xv1/watcher", "/m/v1/watcher/status", "//v1/watcher/status",
)


@pytest.fixture
def isolated_root(tmp_path):
    for source in (*OUTPUTS, YAML, CHECK, LIBRARY):
        target = tmp_path / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return tmp_path


def _run_check(root=ROOT):
    return subprocess.run(
        ["python3", str(root / CHECK.relative_to(ROOT))],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _render_phase(phase: str, root=ROOT) -> dict[Path, bytes]:
    script = (
        "import json,sys;"
        "sys.path.insert(0,'scripts/contracts');"
        "from watcher_gateway_routes_lib import load_source,render;"
        "data,digest=load_source();"
        "out=render(data,digest,sys.argv[1]);"
        "print(json.dumps({str(path): content.hex() for path, content in out.items()}))"
    )
    result = subprocess.run(
        ["python3", "-c", script, phase],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return {Path(path): bytes.fromhex(blob) for path, blob in json.loads(result.stdout).items()}


def _payload_meta(source: str) -> dict:
    start = source.index("_TEXT = ") + len("_TEXT = ")
    payload_text, _ = json.JSONDecoder().raw_decode(source[start:])
    return json.loads(payload_text)["_meta"]


def _caddy_text() -> str:
    raw = CADDY.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    return raw.decode("ascii")


def test_independent_path_method_expectations():
    text = _caddy_text()
    assert not text.lstrip().startswith("{")
    assert "handle " not in text and "reverse_proxy" not in text
    lines = text.split("\n")
    assert lines[-1] == ""
    body = lines[:-1]
    assert body[0] == "# _generated_from contracts/watcher-gateway-routes.yaml"
    assert body[1].startswith("# _yaml_sha256 ")
    assert body[2] == "# _phase_max P2"
    assert body[3] == "# _format watcher-gateway-caddy-paths.v2"
    paths = body[4:]
    assert paths == list(EXPECTED_P2_LINES)
    for line in paths:
        path, regex, *methods = line.split(" ")
        assert path.startswith("/m/v1/watcher/")
        assert methods == sorted(set(methods))
        assert regex.startswith("^") and regex.endswith("$")
    snippet = SNIPPET.read_text(encoding="ascii")
    assert snippet.split("\n")[3] == "# _format watcher-gateway-caddy-snippet.v2"
    assert snippet.count("\t\tmethod ") == len(paths)
    assert "*" not in snippet
    assert snippet.endswith(V2_TAIL)


def test_snippet_v2_defines_routes_then_direct_guard():
    snippet = SNIPPET.read_text(encoding="ascii")
    top_level = [line for line in snippet.split("\n") if line.startswith("(")]
    assert top_level == ["(watcher_gateway_routes) {", "(watcher_gateway_direct_guard) {"]
    routes = snippet[snippet.index("(watcher_gateway_routes) {\n"):snippet.index("(watcher_gateway_direct_guard) {\n")]
    guard = snippet[snippet.index("(watcher_gateway_direct_guard) {\n"):]
    assert guard == "(watcher_gateway_direct_guard) {\n" + DIRECT_GROUP + "}\n"
    assert routes.endswith(FALLBACK_GROUP + DIRECT_GROUP + "}\n")
    assert routes.count("\n\t@wgw_direct {\n") == 1 and routes.count("\n\t@wgw_fallback {\n") == 1
    assert routes.index("\n\t@wgw_fallback {\n") < routes.index("\n\t@wgw_direct {\n")
    assert routes.rindex("import watcher_gateway_upstream") < routes.index("\n\t@wgw_fallback {\n")
    for forbidden in ("reverse_proxy", "8183", "8186", "Authorization", "header_up", "{env."):
        assert forbidden not in snippet


def test_direct_guard_regex_probes_are_independent():
    import re

    for probe in DIRECT_MATCH:
        assert re.search(DIRECT_REGEX, probe) is not None, repr(probe)
        assert re.search(FALLBACK_REGEX, probe) is None, repr(probe)
    for probe in DIRECT_NO_MATCH:
        assert re.search(DIRECT_REGEX, probe) is None, repr(probe)
    for probe in ("/m/v1/watcher", "/m/v1/watcher/status"):
        assert re.search(DIRECT_REGEX, probe) is None
    # G1 => D: every committed P2 row with values that pass the gateway
    # parameter patterns is inside the direct guard once /m is stripped.
    values = {
        "filename": ("1700000000000-1.png", "1-1.jpg"),
        "account_id": ("account-a", "A.b@c-1"),
        "channel_id": ("-1001234567890", "1"),
        "symbol": ("BTCUSDT",),
    }
    compared = 0
    for line in EXPECTED_P2_LINES:
        template = line.split(" ")[0]
        candidates = [template]
        for name in re.findall(r"\{([^{}]+)\}", template):
            candidates = [c.replace("{" + name + "}", v) for c in candidates for v in ("x", *values[name])]
        for candidate in candidates:
            assert candidate.startswith("/m/v1/watcher/")
            assert re.search(DIRECT_REGEX, candidate[len("/m"):]) is not None, candidate
            compared += 1
    assert compared > len(EXPECTED_P2_LINES)


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("guard group removed from routes", "\t@wgw_direct {\n\t\tpath_regexp ^(?i:/v1/watcher)(?:[/\\n%]|$)\n\t}\n\thandle @wgw_direct {\n\t\trespond 404\n\t}\n}\n(watcher_gateway_direct_guard)", "}\n(watcher_gateway_direct_guard)"),
        ("guard regex without %", "(?:[/\\n%]|$)\n\t}\n\thandle @wgw_direct {\n\t\trespond 404\n\t}\n}\n(watcher_gateway_direct_guard)", "(?:[/\\n]|$)\n\t}\n\thandle @wgw_direct {\n\t\trespond 404\n\t}\n}\n(watcher_gateway_direct_guard)"),
        ("guard regex case sensitive", "}\n(watcher_gateway_direct_guard) {\n\t@wgw_direct {\n\t\tpath_regexp ^(?i:/v1/watcher)", "}\n(watcher_gateway_direct_guard) {\n\t@wgw_direct {\n\t\tpath_regexp ^(/v1/watcher)"),
        ("second snippet renamed", "(watcher_gateway_direct_guard) {", "(watcher_gateway_guard) {"),
        ("format v1", "# _format watcher-gateway-caddy-snippet.v2", "# _format watcher-gateway-caddy-snippet.v1"),
    ],
    ids=["guard-removed", "guard-without-percent", "guard-case-sensitive", "guard-snippet-renamed", "format-v1"],
)
def test_check_fails_on_tampered_snippet_v2(isolated_root, label, old, new):
    snippet = isolated_root / SNIPPET.relative_to(ROOT)
    text = snippet.read_text(encoding="ascii")
    assert text.count(old) == 1, label
    snippet.write_text(text.replace(old, new), encoding="ascii")
    result = _run_check(isolated_root)
    assert result.returncode == 1, label
    assert "DIFF" in result.stdout, label
    assert "ROUTES_DIFF_EMPTY" not in result.stdout, label


def test_check_fails_when_snippets_are_swapped(isolated_root):
    snippet = isolated_root / SNIPPET.relative_to(ROOT)
    text = snippet.read_text(encoding="ascii")
    cut = text.index("(watcher_gateway_routes) {\n")
    guard_at = text.index("(watcher_gateway_direct_guard) {\n")
    swapped = text[:cut] + text[guard_at:] + text[cut:guard_at]
    assert swapped != text and len(swapped) == len(text)
    snippet.write_text(swapped, encoding="ascii")
    result = _run_check(isolated_root)
    assert result.returncode == 1
    assert "DIFF" in result.stdout


def test_p3_paths_excluded():
    text = _caddy_text()
    for path in P3_PATHS:
        assert path not in text
    assert "price-alerts" not in text
    assert "price-monitor" not in text


def test_yaml_sha256_digest_matches_source_and_code_artifacts():
    digest = hashlib.sha256(YAML.read_bytes()).hexdigest()
    text = _caddy_text()
    assert text.split("\n")[1] == f"# _yaml_sha256 {digest}"
    assert text.split("\n")[2] == "# _phase_max P2"
    python_meta = _payload_meta(PYTHON.read_text(encoding="utf-8"))
    javascript_meta = _payload_meta(JAVASCRIPT.read_text(encoding="utf-8"))
    assert python_meta["yaml_sha256"] == digest
    assert javascript_meta["yaml_sha256"] == digest
    assert python_meta["phase_max"] == javascript_meta["phase_max"] == "P2"


def test_check_fails_on_tampered_caddy_artifact(isolated_root):
    caddy = isolated_root / CADDY.relative_to(ROOT)
    caddy.write_bytes(caddy.read_bytes() + b"# tampered\n")
    result = _run_check(isolated_root)
    assert result.returncode == 1
    assert "DIFF" in result.stdout
    assert "ROUTES_DIFF_EMPTY" not in result.stdout


def test_check_fails_on_tampered_caddy_snippet(isolated_root):
    snippet = isolated_root / SNIPPET.relative_to(ROOT)
    snippet.write_bytes(snippet.read_bytes().replace(b"respond 404", b"respond 200"))
    result = _run_check(isolated_root)
    assert result.returncode == 1
    assert "DIFF" in result.stdout
    assert "ROUTES_DIFF_EMPTY" not in result.stdout


@pytest.mark.parametrize(
    ("check_id", "edits"),
    [
        ("S-21", [
            ('  - {inner_path: /healthz, methods: "*", reason: "容器健康探针，不经网关"}\n',
             '  - {inner_path: /healthz, methods: "*", reason: "容器健康探针，不经网关"}\n  - {inner_path: /healthz, methods: "*", reason: "dup"}\n'),
        ]),
        ("S-22", [("  caddy_external_prefix: /m\n", "  caddy_external_prefix: /M\n")]),
        ("S-23", [
            ("  source: {type: string, enum: [watcher, v3], gateway_enum: [v3]}   #",
             "  source: {type: string, enum: [watcher, v3], gateway_enum: [v3, watcher]}   #"),
        ]),
        ("S-11", [
            ("  environment: {type: string, enum: [mainnet, testnet]}\n  position_ref",
             "  environment: {type: string, enum: [mainnet, testnet], gateway_enum: [zzz]}\n  position_ref"),
        ]),
        ("S-10", [
            ("  channel: {type: string, pattern: '^-?[0-9]{1,24}$'}\n  hours:",
             "  channel: {type: string, pattern: '^-?[0-9]{1,24}$'}\n  café: {type: string, max_length: 8}\n  hours:"),
            ("    query: [channel, hours, limit, before_created_at, before_id]\n    body: {allow: [], deny: [api_key, api_secret, apiKey, apiSecret, api_id, api_hash, apiId, apiHash, session, sessionString, password, phoneNumber, phone, code, token], required: []}\n    response: {omit: [], mask: [], headers: json}\n    budget: config\n    write: null\n\n  - id: gw.briefings.get",
             "    query: [channel, hours, limit, before_created_at, before_id, café]\n    body: {allow: [], deny: [api_key, api_secret, apiKey, apiSecret, api_id, api_hash, apiId, apiHash, session, sessionString, password, phoneNumber, phone, code, token], required: []}\n    response: {omit: [], mask: [], headers: json}\n    budget: config\n    write: null\n\n  - id: gw.briefings.get"),
        ]),
    ],
)
def test_source_self_consistency_rejects_tampering(isolated_root, check_id, edits):
    yaml_path = isolated_root / YAML.relative_to(ROOT)
    source = yaml_path.read_text(encoding="utf-8")
    for old, new in edits:
        assert source.count(old) == 1, check_id
        source = source.replace(old, new)
    yaml_path.write_text(source, encoding="utf-8")
    result = _run_check(isolated_root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert check_id in result.stdout + result.stderr
    assert "ROUTES_DIFF_EMPTY" not in result.stdout


def test_p3_gate_rejects_p3_paths_in_p2_artifact(isolated_root):
    caddy = isolated_root / CADDY.relative_to(ROOT)
    p3 = _render_phase("P3", isolated_root)[caddy]
    assert all(path.encode("ascii") in p3 for path in P3_PATHS)
    assert caddy.read_bytes() != p3
    caddy.write_bytes(p3)
    result = _run_check(isolated_root)
    assert result.returncode == 1
    assert "DIFF" in result.stdout
    assert "ROUTES_DIFF_EMPTY" not in result.stdout
    assert "PHASE_MAX_NOT_P2" not in result.stdout
    result = _run_check()
    assert result.returncode == 0, result.stdout + result.stderr
    last = result.stdout.splitlines()[-1]
    assert last.startswith("ROUTES_DIFF_EMPTY ")
    assert "phase_max=P2" in last
    assert f"yaml_sha256={hashlib.sha256(YAML.read_bytes()).hexdigest()}" in last


def test_default_check_p2_gate_rejects_consistent_p3_artifacts(isolated_root):
    outputs = tuple(isolated_root / path.relative_to(ROOT) for path in OUTPUTS)
    rendered = _render_phase("P3", isolated_root)
    assert set(rendered) == set(outputs)
    for path in outputs:
        assert rendered[path] != path.read_bytes()
        path.write_bytes(rendered[path])
    result = _run_check(isolated_root)
    assert result.returncode == 1
    assert "PHASE_MAX_NOT_P2" in result.stdout
    assert "phase_max=P3" in result.stdout
    assert "ROUTES_DIFF_EMPTY" not in result.stdout
