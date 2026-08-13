from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "hk-deploy-20260803.sh"


def _function_source(name: str) -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _definitions(*names: str) -> str:
    return "\n".join(_function_source(name) for name in names)


def _run_bash(
    source: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"set -Eeuo pipefail\n{source}"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_environment(
    root: Path,
    *,
    mode: str,
    expected_ip: str = "203.0.113.27",
    proxy_url: str = "",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env.v3").write_text(
        "\n".join(
            (
                f"BINANCE_EGRESS_MODE={mode}",
                f"BINANCE_EXPECTED_EGRESS_IP={expected_ip}",
                f"BINANCE_PROXY_URL={proxy_url}",
                "",
            )
        ),
        encoding="utf-8",
    )


def _loader_source(root: Path) -> str:
    return (
        _definitions("die", "load_binance_egress_settings")
        + f"""
T={root}
BINANCE_EGRESS_MODE=
BINANCE_PROXY_URL=
BINANCE_EXPECTED_EGRESS_IP=
load_binance_egress_settings
printf 'mode=%s\\n' "$BINANCE_EGRESS_MODE"
printf 'proxy=%s\\n' "$BINANCE_PROXY_URL"
printf 'expected=%s\\n' "$BINANCE_EXPECTED_EGRESS_IP"
"""
    )


@pytest.mark.parametrize(
    ("mode", "proxy_url"),
    (
        ("route", ""),
        ("proxy", "http://proxy.internal:3128"),
    ),
)
def test_load_binance_egress_settings_accepts_explicit_modes(
    tmp_path: Path,
    mode: str,
    proxy_url: str,
) -> None:
    root = tmp_path / "runtime"
    _write_environment(root, mode=mode, proxy_url=proxy_url)

    result = _run_bash(_loader_source(root))

    assert result.returncode == 0, result.stderr
    assert f"mode={mode}\n" in result.stdout
    assert f"proxy={proxy_url}\n" in result.stdout
    assert "expected=203.0.113.27\n" in result.stdout


@pytest.mark.parametrize(
    ("mode", "expected_ip", "proxy_url", "message"),
    (
        ("", "203.0.113.27", "", "BINANCE_EGRESS_MODE"),
        ("route", "not-an-ip", "", "BINANCE_EXPECTED_EGRESS_IP"),
        (
            "route",
            "203.0.113.27",
            "http://proxy.internal:3128",
            "must be empty in route mode",
        ),
        ("proxy", "203.0.113.27", "", "must be an http(s) URL"),
        (
            "proxy",
            "203.0.113.27",
            "http://operator:secret@proxy.internal:3128",
            "must not contain credentials",
        ),
    ),
)
def test_load_binance_egress_settings_fails_closed(
    tmp_path: Path,
    mode: str,
    expected_ip: str,
    proxy_url: str,
    message: str,
) -> None:
    root = tmp_path / "runtime"
    _write_environment(
        root,
        mode=mode,
        expected_ip=expected_ip,
        proxy_url=proxy_url,
    )

    result = _run_bash(_loader_source(root))

    assert result.returncode != 0
    assert message in result.stderr


def _write_route_fakes(bin_dir: Path) -> None:
    ip_command = bin_dir / "ip"
    ip_command.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'ip %s\\n' "$*" >>"$FAKE_COMMAND_LOG"
if [ "$*" = "-o link show dev wg0" ]; then
  if [ "${FAKE_WG0_STATE:-up}" = "missing" ]; then
    exit 1
  fi
  if [ "${FAKE_WG0_STATE:-up}" = "down" ]; then
    printf '7: wg0: <POINTOPOINT,NOARP> mtu 1420 state DOWN\\n'
    exit 0
  fi
  printf '7: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420 state UNKNOWN\\n'
  exit 0
fi
if [ "$1" = "-4" ] && [ "$2" = "route" ] && [ "$3" = "get" ]; then
  address="$4"
  interface=wg0
  if [ "${FAKE_BAD_ROUTE_ADDRESS:-}" = "$address" ]; then
    interface=eth0
  fi
  printf '%s via 10.9.0.1 dev %s src 10.9.0.2\\n' \
    "$address" "$interface"
  exit 0
fi
exit 64
""",
        encoding="utf-8",
    )
    ip_command.chmod(0o755)

    getent_command = bin_dir / "getent"
    getent_command.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'getent %s\\n' "$*" >>"$FAKE_COMMAND_LOG"
[ "$1" = "ahostsv4" ]
[ "$2" = "fapi.binance.com" ]
cat "$FAKE_DNS_RECORDS"
""",
        encoding="utf-8",
    )
    getent_command.chmod(0o755)

    curl_command = bin_dir / "curl"
    curl_command.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'curl %s\\n' "$*" >>"$FAKE_COMMAND_LOG"
case "$*" in
  *https://api.ipify.org)
    printf '%s' "${FAKE_ACTUAL_EGRESS_IP:-203.0.113.27}"
    ;;
  *https://fapi.binance.com/fapi/v1/time)
    printf '%s' "${FAKE_FAPI_HTTP_CODE:-200}"
    ;;
  *)
    exit 64
    ;;
esac
""",
        encoding="utf-8",
    )
    curl_command.chmod(0o755)


def _route_environment(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_route_fakes(bin_dir)
    dns_records = tmp_path / "dns-records"
    dns_records.write_text(
        "198.51.100.10 STREAM fapi.binance.com\n"
        "198.51.100.11 STREAM fapi.binance.com\n"
        "198.51.100.10 DGRAM fapi.binance.com\n",
        encoding="utf-8",
    )
    command_log = tmp_path / "commands.log"
    command_log.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_COMMAND_LOG": str(command_log),
        "FAKE_DNS_RECORDS": str(dns_records),
    }


def _route_source() -> str:
    return (
        _definitions("die", "verify_binance_route_egress")
        + """
BINANCE_ROUTE_INTERFACE=wg0
BINANCE_EXPECTED_EGRESS_IP=203.0.113.27
verify_binance_route_egress
"""
    )


def test_route_mode_verifies_every_resolved_ipv4_and_fapi_200(
    tmp_path: Path,
) -> None:
    env = _route_environment(tmp_path)

    result = _run_bash(_route_source(), env)

    assert result.returncode == 0, result.stderr
    log = Path(env["FAKE_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert log.count("ip -4 route get 198.51.100.10\n") == 1
    assert log.count("ip -4 route get 198.51.100.11\n") == 1
    assert "curl --fail --silent --show-error --max-time 15 --interface wg0" in log
    assert "https://api.ipify.org" in log
    assert "--write-out %{http_code} --interface wg0" in log
    assert "https://fapi.binance.com/fapi/v1/time" in log


@pytest.mark.parametrize(
    ("environment", "message"),
    (
        (
            {"FAKE_ACTUAL_EGRESS_IP": "198.51.100.200"},
            "route egress differs from expected JP IP",
        ),
        (
            {"FAKE_BAD_ROUTE_ADDRESS": "198.51.100.11"},
            "IPv4 route does not use wg0",
        ),
        (
            {"FAKE_FAPI_HTTP_CODE": "451"},
            "did not return HTTP 200",
        ),
        (
            {"FAKE_WG0_STATE": "down"},
            "route interface wg0 is down",
        ),
    ),
)
def test_route_mode_fails_closed(
    tmp_path: Path,
    environment: dict[str, str],
    message: str,
) -> None:
    env = _route_environment(tmp_path)
    env.update(environment)

    result = _run_bash(_route_source(), env)

    assert result.returncode != 0
    assert message in result.stderr


def test_route_mode_rejects_empty_ipv4_resolution(tmp_path: Path) -> None:
    env = _route_environment(tmp_path)
    Path(env["FAKE_DNS_RECORDS"]).write_text("", encoding="utf-8")

    result = _run_bash(_route_source(), env)

    assert result.returncode != 0
    assert "has no current IPv4 addresses" in result.stderr


def test_proxy_mode_preserves_http_proxy_egress_check(
    tmp_path: Path,
) -> None:
    env = _route_environment(tmp_path)
    source = (
        _definitions("die", "verify_binance_proxy_egress")
        + """
BINANCE_PROXY_URL=http://proxy.internal:3128
BINANCE_EXPECTED_EGRESS_IP=203.0.113.27
verify_binance_proxy_egress
"""
    )

    result = _run_bash(source, env)

    assert result.returncode == 0, result.stderr
    log = Path(env["FAKE_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert "--proxy http://proxy.internal:3128" in log
    assert "https://api.ipify.org" in log


@pytest.mark.parametrize(
    ("mode", "proxy_url", "expected_proxy"),
    (
        ("route", "", None),
        ("proxy", "http://proxy.internal:3128", "http://proxy.internal:3128"),
    ),
)
def test_release_bound_node_config_uses_mode_specific_proxy_value(
    tmp_path: Path,
    mode: str,
    proxy_url: str,
    expected_proxy: bool | str,
) -> None:
    release_tool = tmp_path / "release_manifest.py"
    release_tool.write_text(
        """def _validated_runtime_resources(value, *, label):
    return value
""",
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps({"runtime_resource_contract": {"cpu": 1}}),
        encoding="utf-8",
    )
    source_config = tmp_path / "source.json"
    source_config.write_text(
        json.dumps(
            {
                "binance": {},
                "runtime_resources": {"cpu": 1},
            }
        ),
        encoding="utf-8",
    )
    output_config = tmp_path / "output.json"
    source = (
        _definitions("prepare_release_bound_config_source")
        + f"""
RELEASE_TOOL={release_tool}
LIVE_RISK_POLICY={policy}
BINANCE_EGRESS_MODE={mode}
BINANCE_PROXY_URL={proxy_url}
prepare_release_bound_config_source {source_config} {output_config}
"""
    )

    result = _run_bash(source)

    assert result.returncode == 0, result.stderr
    payload = json.loads(output_config.read_text(encoding="utf-8"))
    # Route egress omits proxy_url entirely: the node rejects a literal
    # false, and only a real proxy string may reach the artifact.
    assert payload["binance"].get("proxy_url") == expected_proxy


def test_deploy_preflight_uses_mode_aware_egress_gate() -> None:
    text = DEPLOY.read_text(encoding="utf-8")

    assert 'BINANCE_ROUTE_INTERFACE="wg0"' in text
    assert "\nload_binance_egress_settings\nverify_binance_egress\n" in text
    assert 'if proxy_url:\n    updated_binance["proxy_url"] = proxy_url' in text
