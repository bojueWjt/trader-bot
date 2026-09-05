from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT / "runtime"))

from intent_execution_inbox import (
    IntentExecutionInboxError,
    IntentExecutionState,
    JsonIntentExecutionInbox,
    management_repair_eligible,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect dispatched management intents. Stop the node before --apply; "
            "cancel_order or management intents expired over 24h become rejected."
        ),
    )
    parser.add_argument(
        "path", type=Path, help="Path to intent_execution_inbox.json",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Back up and reject eligible records",
    )
    args = parser.parse_args(argv)
    if not args.path.is_file():
        parser.error("inbox path must be an existing file")
    inbox = JsonIntentExecutionInbox(args.path)
    now = datetime.now(timezone.utc)
    try:
        records = inbox.records()
        for record in records:
            if record.state is not IntentExecutionState.DISPATCHED:
                continue
            if record.action in {"open_position", "add_position"}:
                continue
            print(
                f"account_id={record.account_id} intent_id={record.intent_id} "
                f"action={record.action} state={record.state.value} "
                f"valid_until={record.intent_payload.get('valid_until', '')} "
                f"eligible={management_repair_eligible(record, now)}"
            )
        if args.apply:
            repaired, backup = inbox.repair_dispatched_management(now=now)
            print(f"rejected={len(repaired)} backup={backup}")
        else:
            print("dry_run=true; use --apply after stopping the node")
    except (IntentExecutionInboxError, OSError) as exc:
        print(f"inbox_repair failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
