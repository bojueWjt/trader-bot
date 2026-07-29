import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hk-hotfix-account-projection-20260729.sh"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_hotfix_scope_is_limited_to_account_data_services() -> None:
    text = _script()

    assert "host/read_api.py" in text
    assert "host/exchange_state_recorder.py" in text
    assert "trader-v3-controlplane trader-v3-exchange-state" in text
    assert "docker start" not in text
    assert "docker stop" not in text
    assert "systemctl start trader-v3-lifecycle-monitor" not in text
    assert "systemctl restart trader-v3-lifecycle-monitor" not in text


def test_hotfix_backs_up_and_rolls_back_both_files() -> None:
    text = _script()

    assert 'BACKUP_ROOT="${BACKUP_ROOT:-$T/backups/account-projection-hotfix-' in text
    assert '"$BACKUP_ROOT/read_api.py"' in text
    assert '"$BACKUP_ROOT/exchange_state_recorder.py"' in text
    assert '"$BACKUP_ROOT/accounts_projection-preimage.json"' in text
    assert "capture_projection_state" in text
    assert "restore_projection_state" in text
    assert "DELETE FROM accounts_projection" in text
    assert "systemctl start trader-v3-exchange-state" in text
    assert "systemctl stop trader-v3-controlplane trader-v3-exchange-state" in text
    assert "restore_backup" in text
    assert "trap on_error ERR" in text


def test_hotfix_verifies_fresh_rows_and_dry_runs_both_accounts() -> None:
    text = _script()

    assert "age_seconds > 180" in text
    assert "updated_at <= watermark" in text
    assert "fetched_at_value <= watermark" in text
    assert 'source != "binance_fapi_account_v3"' in text
    assert "math.isfinite(equity)" in text
    assert "math.isfinite(age_seconds)" in text
    assert '"dry_run": True' in text
    assert 'verify_dry_run("account-a", risk_admin_token)' in text
    assert 'verify_dry_run("account-b", risk_admin_token)' in text
    assert "fresh account projections missing after timeout" in text


def test_hotfix_compiles_and_compares_reviewed_artifacts() -> None:
    text = _script()

    assert "python3 -m py_compile" in text
    assert "require_command cmp" in text
    assert text.count("cmp -s") == 2
    assert 'STAGE_ROOT="$BACKUP_ROOT/reviewed-artifact"' in text
    assert '(cd "$STAGE_ROOT" && sha256sum -c SHA256SUMS)' in text
    assert '"$STAGE_ROOT/host/read_api.py"' in text
    assert '"$STAGE_ROOT/host/exchange_state_recorder.py"' in text


def test_hotfix_requires_account_services_to_already_be_active() -> None:
    text = _script()

    assert "trader-v3-controlplane must be active before hotfix" in text
    assert "trader-v3-exchange-state must be active before hotfix" in text
    assert "trader-v3-lifecycle-monitor must be stopped before hotfix" in text
    assert "for container in trader-v3-node-a trader-v3-node-b" in text
    assert '"$container must be stopped before hotfix"' in text


def test_hotfix_embedded_python_compiles() -> None:
    snippets = re.findall(r"<<'PY'\n(.*?)\nPY", _script(), flags=re.DOTALL)

    assert len(snippets) == 4
    for index, snippet in enumerate(snippets):
        compile(snippet, f"hotfix-heredoc-{index}.py", "exec")
