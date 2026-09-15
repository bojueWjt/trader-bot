from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / 'scripts/hk-deploy-20260803.sh'


def function(name):
    source = DEPLOY.read_text()
    start = source.index(name + '() {')
    return source[start:source.index('\n}\n', start) + 3]


def run(tmp_path, definitions, body):
    return subprocess.run(['bash', '-c', 'set -euo pipefail\n' + definitions + '\n' + body],
                          text=True, capture_output=True, cwd=tmp_path)


def test_mapping_matches_packager_and_worker_remains_inert():
    tree = ast.parse((ROOT / 'scripts/make_account_stall_release.py').read_text())
    pairs = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'SIGNAL_RUNTIME_RELEASE_FILES' for t in node.targets))
    script = DEPLOY.read_text()
    block = script.split('SIGNAL_RUNTIME_FILE_MAP=(\n', 1)[1].split('\n)', 1)[0]
    actual = dict(line.strip().strip('"').split('|') for line in block.splitlines())
    expected = {release: '$T/' + source for source, release in pairs}
    expected['host/order_management/protection_watchdog.py'] = '$T/services/control-plane/order_management/protection_watchdog.py'
    expected['host/execution_domain/ownership_ledger.py'] = '$T/packages/execution-domain/execution_domain/ownership_ledger.py'
    expected['host/v3_query.py'] = '$HERMES_V3_TRADER_ROOT/scripts/v3_query.py'
    assert actual == expected
    assert script.index('\nverify_signal_runtime_payload\n') < script.index('\nstop_recreate_nodes\n')
    assert script.index('\nverify_signal_credential_contract\n') < script.index('\nstop_recreate_nodes\n')
    restarts = function('restart_signal_runtime_units')
    assert 'signal-worker' not in restarts
    assert 'systemctl enable' not in restarts
    assert script.index('\nrestart_control_plane_units\n') < script.index('\nrestart_signal_runtime_units\n') < script.index('\nrestart_hermes_units\n')


def test_runtime_payload_install_readback_and_unsafe_parent(tmp_path):
    source = tmp_path / 'staging/host/module.py'
    source.parent.mkdir(parents=True)
    source.write_text('value = 1\n')
    target = tmp_path / 'runtime/new/package/module.py'
    target.parent.mkdir(parents=True)
    target.write_text('old = True\n')
    setup = f'STAGING={shlex.quote(str(tmp_path / "staging"))}\nSIGNAL_RUNTIME_FILE_MAP=("host/module.py|{target}")\n'
    defs = '\n'.join(function(name) for name in ['verify_signal_runtime_payload', 'install_host_python_module', 'install_payload_atomically', 'verify_signal_runtime_installed'])
    result = run(tmp_path, defs, setup + f'verify_signal_runtime_payload\ninstall_host_python_module "{source}" "{target}"\nverify_signal_runtime_installed')
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == source.read_bytes()
    target.unlink()
    target.symlink_to(source)
    result = run(tmp_path, defs, setup + 'verify_signal_runtime_payload')
    assert result.returncode != 0 and 'unsafe' in result.stderr


def credential_gate(tmp_path, *, gateway=None, role=None, issuer=None, installed_key=None, gateway_unset=None):
    runtime = tmp_path / 'runtime'
    (runtime / '.venv-cp/bin').mkdir(parents=True, exist_ok=True)
    (runtime / '.venv-cp/bin/python').symlink_to(sys.executable)
    (runtime / '.env.v3').write_text('RISK_ADMIN_TOKEN=unused-host-token\n')
    staging = tmp_path / 'staging'
    staging.mkdir()
    (staging / 'host').symlink_to(ROOT / 'services/control-plane', target_is_directory=True)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    config = {'gateway': gateway or {}, 'role': role or {'RISK_ADMIN_TOKEN': 'operator-secret', 'VIEWER_TOKEN': 'reader-secret'}}
    config['gateway_unset'] = gateway_unset or []
    config['issuer'] = issuer if issuer is not None else [{'State': {'Running': True}, 'Config': {'Env': ['AUTH_SECRET_KEY=issuer-secret', 'OTHER_PRIVATE_KEY=not-copied']}}]
    if installed_key is not None:
        jwt_target = runtime / 'secrets/control-plane/dashboard-jwt.env'
        jwt_target.parent.mkdir(parents=True)
        jwt_target.write_text('AUTH_SECRET_KEY=' + installed_key + '\n')
    (tmp_path / 'config.json').write_text(json.dumps(config))
    mock = bin_dir / 'systemctl'
    mock.write_text('#!' + sys.executable + '\n' + '''import json, shlex, sys
from pathlib import Path
config=json.loads(Path("config.json").read_text())
if "--property=Environment" in sys.argv:
    env=config["gateway" if "hermes-gateway-trader.service" in sys.argv else "role"]
    print(" ".join(shlex.quote(k+"="+v) for k,v in env.items()))
elif "--property=UnsetEnvironment" in sys.argv and "hermes-gateway-trader.service" in sys.argv:
    print(" ".join(shlex.quote(value) for value in config["gateway_unset"]))
''')
    mock.chmod(0o755)
    docker = bin_dir / 'docker'
    docker.write_text('#!' + sys.executable + '\n' + 'import json\nfrom pathlib import Path\nprint(json.dumps(json.loads(Path("config.json").read_text())["issuer"]))\n')
    docker.chmod(0o755)
    setup = f'export PATH="{bin_dir}:$PATH"\nT="{runtime}"\nSTAGING="{staging}"\nTEMP_FILES=()\nHERMES_CLI_ENV_TARGET="{runtime}/secrets/hermes-cli.env"\nHERMES_CLI_DROPIN_TARGET="{runtime}/systemd/cli.conf"\nOPERATOR_JWT_ENV_TARGET="{runtime}/secrets/control-plane/dashboard-jwt.env"\nOPERATOR_JWT_DROPIN_TARGET="{runtime}/systemd/operator-jwt.conf"\nDASHBOARD_ISSUER_CONTAINER=trader-api-1\n'
    return run(tmp_path, function('verify_signal_credential_contract'), setup + 'verify_signal_credential_contract\ncp "$HERMES_CLI_ENV_STAGED" candidate.env\ncp "$HERMES_CLI_DROPIN_STAGED" candidate.conf\ncp "$OPERATOR_JWT_ENV_STAGED" jwt.env\ncp "$OPERATOR_JWT_DROPIN_STAGED" jwt.conf')


def test_credentials_missing_gateway_prepares_minimal_injection_without_exposing_tokens(tmp_path):
    result = credential_gate(tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'minimal Hermes CLI and operator-query JWT injection prepared' in result.stdout
    assert 'operator-secret' not in result.stdout + result.stderr
    candidate = tmp_path / 'candidate.env'
    assert candidate.read_text() == 'RISK_ADMIN_TOKEN=operator-secret\nV3_CONTROL_PLANE_URL=http://127.0.0.1:8183\n'
    assert candidate.stat().st_mode & 0o777 == 0o600


def test_credentials_use_effective_role_and_allow_operator_read_fallback(tmp_path):
    result = credential_gate(tmp_path, gateway={'RISK_ADMIN_TOKEN': 'operator-secret'})
    assert result.returncode == 0, result.stderr


def test_credentials_collision_reports_names_only(tmp_path):
    result = credential_gate(tmp_path, role={'RISK_ADMIN_TOKEN': 'same-secret', 'VIEWER_TOKEN': 'same-secret'})
    assert result.returncode != 0
    assert 'RISK_ADMIN_TOKEN, VIEWER_TOKEN' in result.stderr
    assert 'same-secret' not in result.stderr


def test_existing_producers_restart_only_if_active(tmp_path):
    body = '''SIGNAL_RUNTIME_RESTART_REQUIRED=1
SIGNAL_RUNTIME_RESTARTED_UNITS=()
verify_maintenance_fence() { :; }
die() { echo "$*" >&2; exit 1; }
systemctl() {
  if [ "$1" = is-active ]; then
    [ "${@: -1}" = trader-v3-ingress.service ]
  else
    echo "$*" >> restarted
  fi
}
restart_signal_runtime_units
'''
    result = run(tmp_path, function('restart_signal_runtime_units'), body)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'restarted').read_text() == 'restart trader-v3-ingress.service\n'


def test_existing_mismatched_read_token_blocks(tmp_path):
    result = credential_gate(tmp_path, gateway={'RISK_ADMIN_TOKEN': 'operator-secret', 'V3_READ_TOKEN': 'stale-reader-secret'})
    assert result.returncode != 0
    assert 'V3_READ_TOKEN/read fallback' in result.stderr
    assert 'stale-reader-secret' not in result.stderr


def test_same_cli_environment_does_not_reload_or_restart(tmp_path):
    env = tmp_path / 'cli.env'
    dropin = tmp_path / 'cli.conf'
    env.write_text('RISK_ADMIN_TOKEN=fixture\n')
    dropin.write_text('[Service]\nEnvironmentFile=fixture\n')
    body = f'HERMES_CLI_ENV_STAGED="{env}"\nHERMES_CLI_ENV_TARGET="{env}"\nHERMES_CLI_DROPIN_STAGED="{dropin}"\nHERMES_CLI_DROPIN_TARGET="{dropin}"\n' + "systemctl() { exit 99; }\ninstall_hermes_cli_environment"
    result = run(tmp_path, function('install_hermes_cli_environment'), body)
    assert result.returncode == 0, result.stderr


def test_cli_install_uses_atomic_private_files_and_reload_once(tmp_path):
    env_source, env_target = tmp_path / 'new.env', tmp_path / 'live.env'
    dropin_source, dropin_target = tmp_path / 'new.conf', tmp_path / 'live.conf'
    env_source.write_text('RISK_ADMIN_TOKEN=fixture-token\nV3_CONTROL_PLANE_URL=http://127.0.0.1:8183\n')
    dropin_source.write_text('[Service]\nEnvironmentFile=fixture\n')
    for target in (env_target, dropin_target):
        target.write_text('old-content\n')
    body = f'HERMES_CLI_ENV_STAGED="{env_source}"\nHERMES_CLI_ENV_TARGET="{env_target}"\nHERMES_CLI_DROPIN_STAGED="{dropin_source}"\nHERMES_CLI_DROPIN_TARGET="{dropin_target}"\n' + "systemctl() { echo \"$*\" >> calls; }\ninstall_hermes_cli_environment\ninstall_hermes_cli_environment"
    defs = function('install_hermes_cli_environment') + '\n' + function('install_payload_atomically')
    result = run(tmp_path, defs, body)
    assert result.returncode == 0, result.stderr
    for source, target in [(env_source, env_target), (dropin_source, dropin_target)]:
        assert target.read_bytes() == source.read_bytes()
        assert target.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / 'calls').read_text() == 'daemon-reload\n'


def test_jwt_injection_copies_only_existing_issuer_key(tmp_path):
    result = credential_gate(tmp_path)
    assert result.returncode == 0, result.stderr
    candidate = tmp_path / 'jwt.env'
    assert candidate.read_text() == 'AUTH_SECRET_KEY=issuer-secret\n'
    assert candidate.stat().st_mode & 0o777 == 0o600
    assert 'secrets/control-plane/dashboard-jwt.env' in (tmp_path / 'jwt.conf').read_text()
    assert 'AUTH_SECRET_KEY' not in (tmp_path / 'candidate.env').read_text()
    assert 'issuer-secret' not in result.stdout + result.stderr
    assert 'not-copied' not in result.stdout + result.stderr + candidate.read_text()


def test_matching_effective_jwt_key_does_not_add_a_second_source(tmp_path):
    result = credential_gate(tmp_path, role={'RISK_ADMIN_TOKEN': 'operator-secret', 'AUTH_SECRET_KEY': 'issuer-secret'})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'jwt.env').read_bytes() == b''
    assert (tmp_path / 'jwt.conf').read_bytes() == b''
    body = f'OPERATOR_JWT_ENV_STAGED="{tmp_path / "jwt.env"}"\n' + 'systemctl() { exit 99; }\ninstall_operator_jwt_environment'
    install = run(tmp_path, function('install_operator_jwt_environment'), body)
    assert install.returncode == 0, install.stderr


@pytest.mark.parametrize('location', ['effective', 'installed'])
def test_jwt_key_conflict_is_rejected_without_exposing_either_key(tmp_path, location):
    kwargs = {'role': {'RISK_ADMIN_TOKEN': 'operator-secret', 'AUTH_SECRET_KEY': 'different-secret'}} if location == 'effective' else {'installed_key': 'different-secret'}
    result = credential_gate(tmp_path, **kwargs)
    assert result.returncode != 0
    assert 'differs' in result.stderr
    assert 'different-secret' not in result.stdout + result.stderr
    assert 'issuer-secret' not in result.stdout + result.stderr


@pytest.mark.parametrize('env,running', [([], True), (['AUTH_SECRET_KEY='], True), (['AUTH_SECRET_KEY=one', 'AUTH_SECRET_KEY=two'], True), (['AUTH_SECRET_KEY=bad\nkey'], True), (['AUTH_SECRET_KEY=issuer-secret'], False)])
def test_invalid_issuer_configuration_is_rejected_before_install(tmp_path, env, running):
    result = credential_gate(tmp_path, issuer=[{'State': {'Running': running}, 'Config': {'Env': env}}])
    assert result.returncode != 0
    assert 'dashboard JWT issuer' in result.stderr
    assert not (tmp_path / 'runtime/secrets/control-plane/dashboard-jwt.env').exists()


def test_jwt_install_uses_existing_backup_restore_and_no_extra_restart(tmp_path):
    env_source, env_target = tmp_path / 'jwt.new', tmp_path / 'jwt.live'
    dropin_source, dropin_target = tmp_path / 'jwt-conf.new', tmp_path / 'jwt-conf.live'
    env_source.write_text('AUTH_SECRET_KEY=issuer-secret\n')
    dropin_source.write_text('[Service]\nEnvironmentFile=fixture\n')
    env_target.write_text('prior-env\n')
    dropin_target.write_text('prior-dropin\n')
    backup = tmp_path / 'backup'
    (backup / 'files').mkdir(parents=True)
    (backup / 'index.tsv').touch()
    (backup / 'new-files.txt').touch()
    body = f'OPERATOR_JWT_ENV_STAGED="{env_source}"\nOPERATOR_JWT_ENV_TARGET="{env_target}"\nOPERATOR_JWT_DROPIN_STAGED="{dropin_source}"\nOPERATOR_JWT_DROPIN_TARGET="{dropin_target}"\nBACKUP_ROOT="{backup}"\n' + r'''
BACKUP_CAPTURED=1
FILES_INSTALLED=1
bk() { cp -a "$1" "$BACKUP_ROOT/files/$2"; printf '%s\t%s\n' "$2" "$1" >> "$BACKUP_ROOT/index.tsv"; }
systemctl() { echo "$*" >> calls; }
backup_operator_jwt_environment
install_operator_jwt_environment
install_operator_jwt_environment
restore_installed_runtime_files
'''
    defs = '\n'.join(function(name) for name in ['backup_operator_jwt_environment', 'install_operator_jwt_environment', 'install_payload_atomically', 'restore_installed_runtime_files'])
    result = run(tmp_path, defs, body)
    assert result.returncode == 0, result.stderr
    assert env_target.read_text() == 'prior-env\n'
    assert dropin_target.read_text() == 'prior-dropin\n'
    assert (tmp_path / 'calls').read_text() == 'daemon-reload\n'


@pytest.mark.parametrize('unset', ['RISK_ADMIN_TOKEN', 'V3_CONTROL_PLANE_URL', 'RISK_ADMIN_TOKEN=operator-secret'])
def test_gateway_unset_blocks_injection_before_install(tmp_path, unset):
    result = credential_gate(tmp_path, gateway_unset=[unset])
    assert result.returncode != 0
    assert 'UnsetEnvironment blocks CLI' in result.stderr
    assert 'operator-secret' not in result.stdout + result.stderr
    assert not (tmp_path / 'runtime/secrets/hermes-cli.env').exists()


def test_prepared_credentials_use_private_files_outside_payload_and_backup(tmp_path):
    result = credential_gate(tmp_path)
    assert result.returncode == 0, result.stderr
    prepared = list(tmp_path.glob('.trader-credential.*'))
    assert len(prepared) == 4
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in prepared)
    assert not list((tmp_path / 'staging').glob('.trader-credential.*'))
