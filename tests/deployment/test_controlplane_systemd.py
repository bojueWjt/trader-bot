from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT = REPO_ROOT / "infra" / "systemd" / "trader-v3-controlplane.service"


def _service_directives() -> dict[str, str]:
    directives = {}
    section = ""
    for raw_line in UNIT.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line
            continue
        if section != "[Service]" or not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            directives[key] = value
    return directives


def test_controlplane_unit_bounds_graceful_shutdown() -> None:
    text = UNIT.read_text(encoding="utf-8")
    directives = _service_directives()

    assert (
        "Description=Trader v3 control-plane read API "
        "(isolated, 127.0.0.1:8080)"
    ) in text
    assert "After=docker.service network.target" in text
    assert "[Install]\nWantedBy=multi-user.target" in text
    assert directives["WorkingDirectory"] == (
        "/srv/trader-v3/services/control-plane/api"
    )
    assert directives["EnvironmentFile"] == "/srv/trader-v3/.env.v3"
    assert directives["KillMode"] == "control-group"
    assert directives["TimeoutStopSec"] == "20s"
    assert directives["Restart"] == "on-failure"
    assert directives["RestartSec"] == "3"
    assert directives["ExecStart"] == (
        "/srv/trader-v3/.venv-cp/bin/uvicorn read_api:app "
        "--host 127.0.0.1 --port 8080 "
        "--timeout-graceful-shutdown 10"
    )
