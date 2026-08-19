#!/srv/trader-v3/.venv-cp/bin/python
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


RUNNER_PATH = Path(
    "/srv/trader-v3/account-a-canary/tools/"
    "jp24_live_four_account_20260818.py"
)
NODES = (
    "trader-v3-node-a",
    "trader-v3-node-b",
    "trader-v3-node-c",
    "trader-v3-node-d",
)
FORBIDDEN_ACTIONS = {
    "create",
    "destroy",
    "die",
    "kill",
    "pause",
    "rename",
    "restart",
    "start",
    "stop",
    "unpause",
}


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "jp24_gate_failure_probe",
        RUNNER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load jp24 runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def heartbeat_state(runner) -> dict[str, dict]:
    conn = runner.db_connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT account_id,
                       status,
                       heartbeat_sequence,
                       release_id,
                       image_digest,
                       extract(
                           epoch FROM clock_timestamp() - last_seen_at
                       )
                FROM node_heartbeats
                WHERE account_id = ANY(%s)
                ORDER BY account_id
                """,
                (list(runner.FLEET_ACCOUNTS),),
            )
            rows = cursor.fetchall()
    finally:
        conn.close()
    return {
        str(row[0]): {
            "status": str(row[1]),
            "sequence": int(row[2]),
            "release_id": str(row[3]),
            "image_digest": str(row[4]),
            "age_seconds": round(float(row[5]), 6),
        }
        for row in rows
    }


def container_state() -> dict[str, dict]:
    completed = subprocess.run(
        ["docker", "inspect", *NODES],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = json.loads(completed.stdout)
    return {
        str(row["Name"]).lstrip("/"): {
            "id": str(row["Id"]),
            "started_at": str(row["State"]["StartedAt"]),
            "running": bool(row["State"]["Running"]),
        }
        for row in rows
    }


def node_events(started_at: datetime, ended_at: datetime) -> list[dict]:
    completed = subprocess.run(
        [
            "docker",
            "events",
            "--since",
            started_at.isoformat(),
            "--until",
            ended_at.isoformat(),
            "--filter",
            "type=container",
            "--format",
            "{{json .}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    events = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        actor = event.get("Actor")
        attributes = {}
        if isinstance(actor, dict):
            raw_attributes = actor.get("Attributes")
            if isinstance(raw_attributes, dict):
                attributes = raw_attributes
        name = str(attributes.get("name") or "")
        action = str(event.get("Action") or "")
        if name in NODES and action in FORBIDDEN_ACTIONS:
            events.append(
                {
                    "name": name,
                    "action": action,
                    "time_nano": event.get("timeNano"),
                }
            )
    return events


def wait_for_growth(
    runner,
    before: dict[str, dict],
    timeout_seconds: float = 30,
) -> dict[str, dict]:
    deadline = time.monotonic() + timeout_seconds
    current = heartbeat_state(runner)
    while time.monotonic() < deadline:
        if all(
            current[account_id]["sequence"]
            > before[account_id]["sequence"]
            for account_id in before
        ):
            return current
        time.sleep(0.5)
        current = heartbeat_state(runner)
    raise RuntimeError(
        "heartbeats did not all grow: "
        + json.dumps(
            {
                "before": before,
                "after": current,
            },
            sort_keys=True,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument(
        "--gate",
        choices=(
            "ledger",
            "image",
            "fence",
            "capacity",
            "capacity-age",
            "disk",
            "trust",
            "reviewer-trust",
            "rollout",
            "account-b-evidence-refresh",
        ),
        required=True,
    )
    args = parser.parse_args()
    release_dir = args.release_dir.resolve()
    deploy_script = release_dir / "hk-deploy-20260803.sh"
    if not deploy_script.is_file():
        raise RuntimeError("deployment script is missing")

    runner = load_runner()
    before_heartbeat = heartbeat_state(runner)
    before_containers = container_state()
    started_at = datetime.now(timezone.utc)
    env = dict(os.environ)
    env["DEPLOY_ALLOW_GATE_FAILURE_INJECTION"] = "1"
    hard_gates = {"ledger", "image", "fence"}
    if args.gate in hard_gates:
        env["DEPLOY_INJECT_GATE_FAILURE"] = args.gate
    else:
        env["DEPLOY_INJECT_DEGRADED_GATE"] = args.gate
    deploy_phase = "preflight"
    if args.gate in {"fence", "rollout"}:
        deploy_phase = "execute"
    completed = subprocess.run(
        ["bash", str(deploy_script), deploy_phase],
        cwd=release_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    if args.gate in hard_gates and completed.returncode == 0:
        raise RuntimeError("injected preflight unexpectedly succeeded")
    if args.gate not in hard_gates and completed.returncode != 0:
        raise RuntimeError(
            f"injected degraded gate failed deployment: rc={completed.returncode}"
        )

    after_heartbeat = wait_for_growth(runner, before_heartbeat)
    ended_at = datetime.now(timezone.utc)
    after_containers = container_state()
    if after_containers != before_containers:
        raise RuntimeError("A-D container identity or start time changed")
    if any(
        row["status"] != before_heartbeat[account_id]["status"]
        for account_id, row in after_heartbeat.items()
    ):
        raise RuntimeError("A-D heartbeat status changed")
    events = node_events(started_at, ended_at)
    if events:
        raise RuntimeError(
            "A-D container mutation events observed: "
            + json.dumps(events, sort_keys=True)
        )

    output_lines = (
        completed.stdout.splitlines()
        + completed.stderr.splitlines()
    )
    print(
        json.dumps(
            {
                "gate": args.gate,
                "deploy_phase": deploy_phase,
                "returncode": completed.returncode,
                "heartbeat_before": {
                    key: value["sequence"]
                    for key, value in before_heartbeat.items()
                },
                "heartbeat_after": {
                    key: value["sequence"]
                    for key, value in after_heartbeat.items()
                },
                "container_mutation_events": events,
                "log_tail": output_lines[-30:],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
