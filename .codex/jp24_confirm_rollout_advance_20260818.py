#!/srv/trader-v3/.venv-cp/bin/python
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


RUNNER_PATH = Path(__file__).with_name("jp24_live_four_account_20260818.py")
NEXT_PHASE_BY_ACCOUNT = {
    "account-a": "account_b_rollout",
    "account-b": "account_c_rollout",
    "account-c": "account_d_rollout",
    "account-d": "fleet_complete",
}


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "jp24_single_account_runner",
        RUNNER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load jp24 single-account runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Advance one jp-24 rollout phase after explicit user confirmation.",
    )
    result.add_argument(
        "--account",
        choices=tuple(NEXT_PHASE_BY_ACCOUNT),
        required=True,
    )
    result.add_argument("--closure-dir", type=Path, required=True)
    result.add_argument("--confirm", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    next_phase = NEXT_PHASE_BY_ACCOUNT[args.account]
    expected_confirmation = f"ADVANCE {args.account} to {next_phase}"
    if args.confirm != expected_confirmation:
        raise RuntimeError(
            f"confirmation mismatch; expected: {expected_confirmation}"
        )

    runner = load_runner()
    target = runner.TARGET_BY_ACCOUNT[args.account]
    account_dir = args.closure_dir.resolve()
    runner.advance_phase(account_dir, target, next_phase)

    conn = runner.db_connect()
    try:
        runner.require_phase(conn, next_phase)
    finally:
        conn.close()
    runner.log(
        f"ROLLOUT_ADVANCED account={args.account} to_phase={next_phase}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
