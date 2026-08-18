from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "hk-deploy-20260803.sh"
JP24_PREPARE = REPO_ROOT / "scripts" / "jp24-p1-prepare.sh"


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
    account_expected_ips: tuple[str, str, str, str] = (
        "203.0.113.27",
        "203.0.113.28",
        "203.0.113.29",
        "203.0.113.30",
    ),
    account_proxy_urls: tuple[str, str, str, str] | None = None,
) -> None:
    account_a, account_b, account_c, account_d = account_expected_ips
    if account_proxy_urls is None:
        account_proxy_urls = ("", "", "", "")
        if mode == "account_networks":
            account_proxy_urls = (
                "",
                "",
                "",
                "http://proxy-d.internal:3128",
            )
    proxy_a, proxy_b, proxy_c, proxy_d = account_proxy_urls
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env.v3").write_text(
        "\n".join(
            (
                f"BINANCE_EGRESS_MODE={mode}",
                f"BINANCE_EXPECTED_EGRESS_IP={expected_ip}",
                f"BINANCE_PROXY_URL={proxy_url}",
                f"BINANCE_PROXY_URL_A={proxy_a}",
                f"BINANCE_PROXY_URL_B={proxy_b}",
                f"BINANCE_PROXY_URL_C={proxy_c}",
                f"BINANCE_PROXY_URL_D={proxy_d}",
                f"BINANCE_EXPECTED_EGRESS_IP_A={account_a}",
                f"BINANCE_EXPECTED_EGRESS_IP_B={account_b}",
                f"BINANCE_EXPECTED_EGRESS_IP_C={account_c}",
                f"BINANCE_EXPECTED_EGRESS_IP_D={account_d}",
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
BINANCE_PROXY_URL_A=
BINANCE_PROXY_URL_B=
BINANCE_PROXY_URL_C=
BINANCE_PROXY_URL_D=
BINANCE_EXPECTED_EGRESS_IP=
BINANCE_EXPECTED_EGRESS_IP_A=
BINANCE_EXPECTED_EGRESS_IP_B=
BINANCE_EXPECTED_EGRESS_IP_C=
BINANCE_EXPECTED_EGRESS_IP_D=
load_binance_egress_settings
printf 'mode=%s\\n' "$BINANCE_EGRESS_MODE"
printf 'proxy=%s\\n' "$BINANCE_PROXY_URL"
printf 'proxy_a=%s\\n' "$BINANCE_PROXY_URL_A"
printf 'proxy_b=%s\\n' "$BINANCE_PROXY_URL_B"
printf 'proxy_c=%s\\n' "$BINANCE_PROXY_URL_C"
printf 'proxy_d=%s\\n' "$BINANCE_PROXY_URL_D"
printf 'expected=%s\\n' "$BINANCE_EXPECTED_EGRESS_IP"
printf 'expected_a=%s\\n' "$BINANCE_EXPECTED_EGRESS_IP_A"
printf 'expected_b=%s\\n' "$BINANCE_EXPECTED_EGRESS_IP_B"
printf 'expected_c=%s\\n' "$BINANCE_EXPECTED_EGRESS_IP_C"
printf 'expected_d=%s\\n' "$BINANCE_EXPECTED_EGRESS_IP_D"
"""
    )


@pytest.mark.parametrize(
    ("mode", "proxy_url"),
    (
        ("route", ""),
        ("proxy", "http://proxy.internal:3128"),
        ("account_networks", ""),
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
    if mode == "account_networks":
        assert "expected_a=203.0.113.27\n" in result.stdout
        assert "expected_b=203.0.113.28\n" in result.stdout
        assert "expected_c=203.0.113.29\n" in result.stdout
        assert "expected_d=203.0.113.30\n" in result.stdout
        assert "proxy_a=\n" in result.stdout
        assert "proxy_b=\n" in result.stdout
        assert "proxy_c=\n" in result.stdout
        assert "proxy_d=http://proxy-d.internal:3128\n" in result.stdout


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
        (
            "account_networks",
            "203.0.113.27",
            "http://proxy.internal:3128",
            "must be empty in account_networks mode",
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


@pytest.mark.parametrize(
    ("account_proxy_urls", "message"),
    (
        (("", "", "", ""), "BINANCE_PROXY_URL_D"),
        (
            (
                "http://proxy-a.internal:3128",
                "",
                "",
                "http://proxy-d.internal:3128",
            ),
            "account A, B, and C Binance proxies must be empty",
        ),
        (
            ("", "", "", "http://operator:secret@proxy-d.internal:3128"),
            "must not contain credentials",
        ),
    ),
)
def test_account_network_settings_require_only_account_d_proxy(
    tmp_path: Path,
    account_proxy_urls: tuple[str, str, str, str],
    message: str,
) -> None:
    root = tmp_path / "runtime"
    _write_environment(
        root,
        mode="account_networks",
        account_proxy_urls=account_proxy_urls,
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


def _write_account_network_docker_fake(bin_dir: Path) -> None:
    docker_command = bin_dir / "docker"
    docker_command.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\\n' "$*" >>"$FAKE_COMMAND_LOG"
if [ "$1" = "network" ] && [ "$2" = "inspect" ]; then
  exit 0
fi
if [ "$1" = "inspect" ]; then
  node="$2"
  suffix="${node##*-}"
  network="trader-v3-account-$suffix"
  if [ "${FAKE_BAD_NETWORK_NODE:-}" = "$node" ]; then
    network=bridge
  fi
  printf '[{"NetworkSettings":{"Networks":{"%s":{}}}}]\\n' "$network"
  exit 0
fi
if [ "$1" = "exec" ]; then
  node="$3"
  proxy_url="$6"
  network="${node/trader-v3-node/trader-v3-account}"
  case "$network" in
    trader-v3-account-a) value=203.0.113.27 ;;
    trader-v3-account-b) value=203.0.113.28 ;;
    trader-v3-account-c) value=203.0.113.29 ;;
    trader-v3-account-d)
      [ "$proxy_url" = "http://proxy-d.internal:3128" ] || exit 64
      value=203.0.113.30
      ;;
    *) exit 64 ;;
  esac
  if [ "${FAKE_BAD_EGRESS_NETWORK:-}" = "$network" ]; then
    value=198.51.100.200
  fi
  status=200
  if [ "${FAKE_BAD_FAPI_NETWORK:-}" = "$network" ]; then
    status=503
  fi
  printf '%s\\t%s\\n' "$value" "$status"
  exit 0
fi
exit 64
""",
        encoding="utf-8",
    )
    docker_command.chmod(0o755)


def _account_network_source() -> str:
    return (
        _definitions(
            "die",
            "probe_existing_node_egress",
            "verify_binance_account_network_egress",
        )
        + """
ALL_NODES=(
  trader-v3-node-a
  trader-v3-node-b
  trader-v3-node-c
  trader-v3-node-d
)
BINANCE_ACCOUNT_NETWORKS=(
  trader-v3-account-a
  trader-v3-account-b
  trader-v3-account-c
  trader-v3-account-d
)
BINANCE_EXPECTED_EGRESS_IP_A=203.0.113.27
BINANCE_EXPECTED_EGRESS_IP_B=203.0.113.28
BINANCE_EXPECTED_EGRESS_IP_C=203.0.113.29
BINANCE_EXPECTED_EGRESS_IP_D=203.0.113.30
BINANCE_PROXY_URL_A=
BINANCE_PROXY_URL_B=
BINANCE_PROXY_URL_C=
BINANCE_PROXY_URL_D=http://proxy-d.internal:3128
verify_binance_account_network_egress
"""
    )


def _account_network_environment(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_account_network_docker_fake(bin_dir)
    command_log = tmp_path / "commands.log"
    command_log.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_COMMAND_LOG": str(command_log),
    }


def test_account_network_mode_verifies_all_nodes_and_egress(
    tmp_path: Path,
) -> None:
    env = _account_network_environment(tmp_path)

    result = _run_bash(_account_network_source(), env)

    assert result.returncode == 0, result.stderr
    log = Path(env["FAKE_COMMAND_LOG"]).read_text(encoding="utf-8")
    assert log.count("docker exec -i trader-v3-node-") == 4
    assert log.count("http://proxy-d.internal:3128") == 1
    assert "docker run " not in log
    assert result.stdout.count("== Binance account network verified:") == 4


@pytest.mark.parametrize(
    ("environment", "message"),
    (
        (
            {"FAKE_BAD_NETWORK_NODE": "trader-v3-node-b"},
            "node network differs",
        ),
        (
            {"FAKE_BAD_EGRESS_NETWORK": "trader-v3-account-c"},
            "account-network egress differs",
        ),
        (
            {"FAKE_BAD_FAPI_NETWORK": "trader-v3-account-d"},
            "did not return HTTP 200",
        ),
    ),
)
def test_account_network_mode_fails_closed(
    tmp_path: Path,
    environment: dict[str, str],
    message: str,
) -> None:
    env = _account_network_environment(tmp_path)
    env.update(environment)

    result = _run_bash(_account_network_source(), env)

    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.parametrize(
    ("mode", "account_id", "proxy_url", "proxy_url_d", "expected_proxy"),
    (
        ("route", "account-a", "", "", None),
        (
            "proxy",
            "account-a",
            "http://proxy.internal:3128",
            "",
            "http://proxy.internal:3128",
        ),
        (
            "account_networks",
            "account-a",
            "",
            "http://proxy-d.internal:3128",
            None,
        ),
        (
            "account_networks",
            "account-d",
            "",
            "http://proxy-d.internal:3128",
            "http://proxy-d.internal:3128",
        ),
    ),
)
def test_release_bound_node_config_uses_mode_specific_proxy_value(
    tmp_path: Path,
    mode: str,
    account_id: str,
    proxy_url: str,
    proxy_url_d: str,
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
                "account_id": account_id,
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
BINANCE_PROXY_URL_A=
BINANCE_PROXY_URL_B=
BINANCE_PROXY_URL_C=
BINANCE_PROXY_URL_D={proxy_url_d}
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
    assert "verify_binance_account_network_egress" in text
    assert 'proxy_url = account_proxy_urls[account_id] or False' in text


def test_jp24_prepare_exposes_internal_services_on_account_networks() -> None:
    text = JP24_PREPARE.read_text(encoding="utf-8")

    assert '"BINANCE_EGRESS_MODE=account_networks"' in text
    assert '"BINANCE_PROXY_URL_D=http://100.107.72.78:13128"' in text
    assert '"BINANCE_EXPECTED_EGRESS_IP_D=103.197.211.79"' in text
    assert "connect_redis_networks() {" in text
    assert "--alias trader-v3-redis" in text
    assert "allow_account_network_control_plane() {" in text
    assert 'ufw status | grep -Fxq \'Status: active\'' in text
    assert 'ufw allow in \\\n      on "$bridge"' in text
    assert (
        "'$4 ~ /^(127\\.0\\.0\\.1|100\\.89\\.58\\.40|"
        "172\\.30\\.[1-4]\\.1):8080$/'"
    ) in text
    for gateway in (
        "172.30.1.1",
        "172.30.2.1",
        "172.30.3.1",
        "172.30.4.1",
    ):
        assert f"http://{gateway}:8080" in text
