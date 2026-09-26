"""Offline transfer format and explicit Codex batch runner.

prompts.jsonl: key, system, user (exact replay inputs), schema_name,
source_version_id, channel_id, channel_name, message_time, text, has_image,
previous_text (one reply parent, context only).
responses.jsonl: key + response {schema_version:2,actions:[...],stats:{...}}, or
abstain {reason_code,note}; numbers are serialized as decimal strings.
review.jsonl: graph_version, source_version_id, channel_id, message_time,
text, has_image, episode_ids, plan_source, is_open, selected_results,
llm_results, parser_results. Results are extracted events (canonical_plans holds selected plans), with checks.op and
checks.refers_to_previous. Missing recordings have llm_results=[]; abstentions
have kind=undecidable and no llm_evidence_valid. v2 actions keep original quotes,
field_issues and time_ref in checks.action. See CX_BATCH_V2.md alongside this file. Sampling is balanced across
channel/open-status strata, total N (not N per channel). No labels are gold.

Only `run` invokes Codex. All other commands are offline. Raw attempts can
contain private messages; keep the batch directory outside the repository.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import tempfile
import time
import uuid

OPS = ("open", "add", "reduce", "take_profit", "stop_loss_hit", "stop_move", "close", "cancel",
       "analysis", "chatter", "result", "undecidable")
OP_KINDS = {"open": {"entry_proposal", "entry_claimed"}, "add": {"add"}, "reduce": {"reduce"},
            "take_profit": {"reduce", "close_claimed"}, "stop_loss_hit": {"close_claimed"},
            "stop_move": {"stop_move"}, "close": {"close_claimed"}, "cancel": {"cancel", "expire"},
            "analysis": {"analysis", "amend", "tp_ladder", "correction", "delete_notice"},
            "chatter": {"chatter"}, "result": {"result_post"}, "undecidable": {"undecidable"}}
from . import cx_v2

BATCH_RULES = cx_v2.RULES


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True, allow_nan=False)


def write_json(path: Path, value):
    _atomic_text(path, dumps(value) + "\n")


def _atomic_text(path: Path, text: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".cx-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_jsonl(path: Path):
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line, parse_float=Decimal)


def stratified(rows, n, seed, key):
    if n is None:
        return rows
    if n < 0:
        raise ValueError("sample must be >= 0")
    rng = random.Random(seed)
    groups = defaultdict(list)
    for row in sorted(rows, key=lambda r: r["source_version_id"]):
        groups[key(row)].append(row)
    keys = sorted(groups)
    rng.shuffle(keys)
    for group in groups.values():
        rng.shuffle(group)
    sampled = []
    while keys and len(sampled) < n:
        for group_key in list(keys):
            sampled.append(groups[group_key].pop())
            if not groups[group_key]:
                keys.remove(group_key)
            if len(sampled) == n:
                break
    return sampled


def export_prompts(layout, output: Path, *, channels=(), sample=None, seed=0, exclude_prompts=()):
    import polars as pl
    from .llm import build_extract_prompt, record_key, SCHEMA_NAME_EXTRACT
    from .sources import canonical_peer_id

    rows = pl.read_parquet(layout.message_version).to_dicts()
    selected = {canonical_peer_id(c, "channel") for c in channels}
    if selected:
        rows = [r for r in rows if r["channel_id"] in selected]
    by_message = defaultdict(list)
    for row in rows:
        by_message[(row["channel_id"], row["source_id"]["message_id"])].append(row)
    counts = {"input": len(rows), "skipped_service": 0, "skipped_empty": 0}
    eligible = []
    for row in rows:
        if row["message_type"] != "message":
            counts["skipped_service"] += 1
        elif not (row["text"] or "").strip():
            counts["skipped_empty"] += 1
        else:
            eligible.append(row)
    excluded_texts = {r["text"] for path in exclude_prompts for r in read_jsonl(path)}
    if exclude_prompts:
        counts["excluded_seen_text"] = sum(r["text"] in excluded_texts for r in eligible)
        eligible = [r for r in eligible if r["text"] not in excluded_texts]
    prompts = []
    for row in stratified(eligible, sample, seed, lambda r: r["channel_id"]):
        date = row["message_date"].isoformat() if row["message_date"] else None
        system, user = build_extract_prompt(row["text"], channel_name=row["channel_name"], message_date=date)
        cutoff = row["available_at"]
        parents = by_message.get((row["channel_id"], row["reply_to_message_id"]), [])
        # Unknown clocks and future edits cannot supply context.
        parents = [p for p in parents if cutoff is not None and p["available_at"] is not None
                   and p["available_at"] <= cutoff and p["source_version_id"] != row["source_version_id"]]
        parent = max(parents, key=lambda p: (p["available_at"], p["version_no"]), default=None)
        system, user = cx_v2.build_prompt(row["text"], channel_name=row["channel_name"], message_date=date, previous_text=parent["text"] if parent else None)
        prompts.append({"key": record_key(system, user, cx_v2.SCHEMA_NAME), "system": system, "user": user,
                        "schema_name": cx_v2.SCHEMA_NAME, "source_version_id": row["source_version_id"],
                        "channel_id": row["channel_id"], "channel_name": row["channel_name"], "message_time": date,
                        "text": row["text"], "has_image": any(re.search(r"photo|image", k, re.I) for k in row["media_kinds"] or []),
                        "previous_text": parent["text"] if parent else None})
    _atomic_text(output, "".join(dumps(p) + "\n" for p in prompts))
    counts.update(eligible=len(eligible), exported=len(prompts), unique_keys=len({p["key"] for p in prompts}), seed=seed)
    write_json(Path(output).with_suffix(".stats.json"), counts)
    return counts


def output_schema():
    return cx_v2.output_schema()

def abstain(note):
    return {"abstain": {"reason_code": "INTENT_AMBIGUOUS", "note": note}}


# Match whole numeric tokens, never a substring (10 in 100), including commas.
NUMBER = re.compile(r"(?<![0-9A-Za-z_.,])[+\-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![0-9A-Za-z_万]|[.,]\d)")


def quote_response(item, text):
    """Strict provider validation then exact Decimal/token match; no span guessing."""
    if "actions" in item or item.get("schema_version") == 2:
        try:
            return {"response": cx_v2.validate_response(item, text)}
        except (ValueError, TypeError, KeyError):
            return abstain("invalid_v2_envelope")
    from .llm import _GrokExtract, validate_evidence
    from pydantic import ValidationError
    try:
        raw = dict(item)
        raw.pop("key")
        quotes = raw.pop("quotes")
        op = raw.pop("op")
        previous = raw.pop("refers_to_previous")
        if op not in OP_KINDS or type(previous) is not bool or raw.get("kind") not in OP_KINDS[op]:
            raise ValueError("op_kind_mismatch")
        if previous and raw["kind"] == "entry_proposal":
            raise ValueError("previous_order_is_not_new_entry")
        payload = _GrokExtract.model_validate({**raw, "spans": []}).model_dump()
        values = {"entry.lo": [], "entry.hi": [], "stop": [], "entries": payload["entries"],
                  "tps": [t["level"] for t in payload["tps"]]}
        if payload["entry"] is not None:
            values["entry.lo"] = [payload["entry"]["lo"]]
            values["entry.hi"] = [payload["entry"]["hi"]]
        if payload["stop"] is not None:
            values["stop"] = [payload["stop"]]
        evidence = defaultdict(list)
        if not isinstance(quotes, list):
            raise ValueError("invalid_quotes")
        for q in quotes:
            if not isinstance(q, dict) or set(q) != {"field", "quote"} or q["field"] not in values:
                raise ValueError("invalid_quote")
            evidence[q["field"]].append(q["quote"])
        for field, numbers in values.items():
            if len(evidence[field]) != len(numbers):
                raise ValueError("quote_count_mismatch")
            for number, quote in zip(numbers, evidence[field]):
                if not isinstance(quote, str) or not quote:
                    raise ValueError("invalid_quote")
                start = text.find(quote)
                if start < 0:
                    raise ValueError("quote_not_in_text")
                matches = [m for m in NUMBER.finditer(quote) if Decimal(m.group().replace(",", "")) == number]
                if not matches:
                    raise ValueError("number_not_in_quote")
                match = matches[0]
                a, b = start + match.start(), start + match.end()
                # A quote may itself cut a token, e.g. quote='10' within text='100'.
                if not any(m.start() == a and m.end() == b for m in NUMBER.finditer(text)):
                    raise ValueError("partial_numeric_token")
                payload["spans"].append({"field": field, "start": a, "end": b})
        if validate_evidence(payload, text):
            raise ValueError("validate_evidence_failed")
        payload.update(op=op, refers_to_previous=previous)
        if payload["kind"] == "undecidable":
            return abstain("model_undecidable")
        return {"response": payload}
    except (ValueError, TypeError, KeyError, ValidationError):
        return abstain("evidence_rejected:invalid_schema_or_quote")


def _tokens(log):
    matches = re.findall(r"tokens used\s*[:\r\n]*\s*([\d,]+)", log, flags=re.I)
    return sum(int(m.replace(",", "")) for m in matches) if matches else None


TRANSPORT_FAILED = "transport_failed_after_retries"


def _transport_failed(record):
    return record.get("abstain", {}).get("note") == TRANSPORT_FAILED


def _run_batch(batch, directory, executable, batch_id, retries, timeout, backoff=0):
    prompt = dumps({"instructions": BATCH_RULES, "messages": batch})
    schema_path = directory / "schema.json"
    attempts = []
    for attempt in range(retries + 1):
        prefix = directory / "raw" / f"{batch_id}-{attempt}"
        out = prefix.with_suffix(".json")
        command = [executable, "exec", "-m", "gpt-6-astra", "-s", "read-only", "--skip-git-repo-check",
                   "--ephemeral", "--output-schema", str(schema_path), "-o", str(out), "-"]
        started = time.monotonic()
        stdout, stderr, error = "", "", None
        try:
            with tempfile.TemporaryDirectory(prefix="cx-exec-") as cwd:
                proc = subprocess.run(command, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                      timeout=timeout, cwd=cwd, check=False)
            stdout, stderr = proc.stdout, proc.stderr
            if proc.returncode != 0:
                raise ValueError("codex_nonzero_exit")
            envelope = json.loads(out.read_text(encoding="utf-8"), parse_float=Decimal)
            items = envelope["items"]
            keys = [i["key"] for i in items]
            if len(keys) != len(batch) or set(keys) != {p["key"] for p in batch}:
                raise ValueError("missing_duplicate_or_unknown_keys")
            texts = {p["key"]: p["text"] for p in batch}
            results = [{"key": i["key"], **quote_response(i, texts[i["key"]])} for i in items]
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            stdout = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else stdout
            stderr = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr
            error = "codex_timeout"
        except (OSError, ValueError, TypeError, KeyError):
            error = "codex_transport_or_batch_format"
        _atomic_text(prefix.with_suffix(".stdout.txt"), stdout)
        _atomic_text(prefix.with_suffix(".stderr.txt"), stderr)
        metric = {"batch_id": batch_id, "attempt": attempt + 1, "elapsed_s": time.monotonic() - started,
                  "tokens_used": _tokens(stdout + "\n" + stderr), "error": error}
        write_json(prefix.with_suffix(".metrics.json"), metric)
        attempts.append(metric)
        if error is None:
            return results, attempts
        if backoff and attempt < retries:
            # "model at capacity" is transient; immediate retries just burn the budget.
            time.sleep(min(600, backoff * 2 ** attempt))
    return [{"key": p["key"], **abstain(TRANSPORT_FAILED)} for p in batch], attempts


def length_batches(rows, batch_size=20, max_chars=12000):
    """Bound serialized message content, never split/truncate a single message."""
    batch, size = [], 0
    for row in rows:
        length = len(dumps(row))
        if batch and (len(batch) >= batch_size or size + length > max_chars):
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += length
    if batch:
        yield batch


def run_batches(prompts: Path, directory: Path, *, executable=None, batch_size=20, max_chars=12000, concurrency=3, retries=2, timeout=600, backoff=0):
    import fcntl
    from .llm import record_key, SCHEMA_NAME_EXTRACT
    if batch_size < 1 or max_chars < 1 or concurrency < 1 or retries < 0 or not math.isfinite(timeout) or timeout <= 0 or backoff < 0:
        raise ValueError("invalid runner limits")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    executable = os.path.expanduser(executable or "~/.local/lib/codex-launch")
    if os.sep in executable:
        executable = str(Path(executable).resolve())
    with (directory / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inputs = {}
        for row in read_jsonl(prompts):
            if row["schema_name"] not in (SCHEMA_NAME_EXTRACT, cx_v2.SCHEMA_NAME) or row["key"] != record_key(row["system"], row["user"], row["schema_name"]):
                raise ValueError("prompt_key_mismatch")
            if json.loads(row["user"])["text"] != row["text"]:
                raise ValueError("prompt_text_mismatch")
            inputs.setdefault(row["key"], row)
        response_path = directory / "responses.jsonl"
        # Atomic per-batch files are the journal. Rebuild the public JSONL after a crash.
        journal = directory / "completed"
        journal.mkdir(exist_ok=True)
        (directory / "raw").mkdir(exist_ok=True)
        completed = {}
        journal_paths = sorted(journal.glob("*.json"))
        if not journal_paths and response_path.exists():
            for row in read_jsonl(response_path):
                completed[row["key"]] = row
            write_json(journal / "seed.json", list(completed.values()))
        for path in journal_paths:
            for row in json.loads(path.read_text(), parse_float=Decimal):
                # Journal file order is arbitrary; an old transport failure never masks a later answer.
                if row["key"] not in completed or _transport_failed(completed[row["key"]]):
                    completed[row["key"]] = row
        if any(completed[k].get("response", {}).get("schema_version") != 2 for k in inputs.keys() & completed.keys() if inputs[k]["schema_name"] == cx_v2.SCHEMA_NAME and "response" in completed[k]):
            raise ValueError("stale_v1_journal_use_new_output_dir")
        # Transport failures carry no model judgement, so a restart asks again.
        pending = [r for k, r in inputs.items() if k not in completed or _transport_failed(completed[k])]
        _atomic_text(response_path, "".join(dumps(completed[k]) + "\n" for k in sorted(completed)))
        write_json(directory / "schema.json", output_schema())
        started = time.monotonic()
        attempts = []
        invocation = uuid.uuid4().hex
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = []
            for index, batch in enumerate(length_batches(pending, batch_size, max_chars)):
                bid = f"{invocation}-{index:06d}"
                future = pool.submit(_run_batch, batch, directory, executable, bid, retries, timeout, backoff)
                futures.append((future, bid))
            ids = {f: bid for f, bid in futures}
            for future in as_completed(ids):
                results, metrics = future.result()
                write_json(journal / f"{ids[future]}.json", results)
                completed.update({r["key"]: r for r in results})
                attempts.extend(metrics)
                # Journal is authoritative; recover a torn append on next startup.
                # Avoid rewriting the entire corpus after each batch (quadratic I/O).
                with response_path.open("a", encoding="utf-8") as stream:
                    stream.write("".join(dumps(row) + "\n" for row in results))
                    stream.flush()
                    os.fsync(stream.fileno())
        _atomic_text(response_path, "".join(dumps(completed[k]) + "\n" for k in sorted(completed)))
        totals = [json.loads(p.read_text()) for p in (directory / "raw").glob("*.metrics.json")]
        report = {"input_keys": len(inputs), "skipped": len(inputs) - len(pending), "completed": len(pending),
                  "calls": len(attempts), "elapsed_s": time.monotonic() - started,
                  "tokens_used": sum(m["tokens_used"] or 0 for m in attempts),
                  "unknown_token_calls": sum(m["tokens_used"] is None for m in attempts),
                  "total_calls": len(totals), "total_tokens_used": sum(m["tokens_used"] or 0 for m in totals),
                  "total_call_elapsed_s": sum(m["elapsed_s"] for m in totals),
                  "abstained": sum("abstain" in completed[k] for k in inputs)}
        stats = [completed[k].get("response", {}).get("stats", {}) for k in inputs]
        report.update(model_uncertain=sum(s.get("model_uncertain", 0) for s in stats),
                      field_evidence_failed=sum(s.get("field_evidence_failed", 0) for s in stats),
                      whole_message_discarded=report["abstained"] + sum(s.get("whole_message_discarded", 0) for s in stats))
        write_json(directory / "stats.json", report)
        return report


def import_responses(responses: Path, output: Path):
    items = {}
    for row in read_jsonl(responses):
        key = row["key"]
        if bool(row.get("response")) == bool(row.get("abstain")):
            raise ValueError("expected response xor abstain")
        record = {k: row[k] for k in ("response", "abstain") if k in row}
        if key in items and items[key] != record:
            raise ValueError("conflicting_duplicate_key")
        items[key] = record
    version = "cx-batch-v2" if any(r.get("response", {}).get("schema_version") == 2 for r in items.values()) else "cx-batch-v1"
    write_json(output, {"version": version, "model": "gpt-6-astra", "items": items})
    return {"imported": len(items), "abstained": sum("abstain" in r for r in items.values())}


def save_build_review(layout, graph_version, cp, mv, episodes, plan_source, *, events=None):
    import polars as pl
    from .plan_source import approved_sources, select_plans, action_overridden
    ex = pl.read_parquet(layout.extracted_event) if layout.extracted_event.exists() else None
    plans, extractions = defaultdict(list), defaultdict(list)
    for row in cp.iter_rows(named=True):
        plans[row["source_version_id"]].append(row)
    if ex is not None:
        for row in ex.iter_rows(named=True):
            row["checks"] = json.loads(row.get("checks") or "{}")
            extractions[row["source_version_id"]].append(row)
    selected = set(select_plans(cp, plan_source, ex)["plan_id"].to_list())
    approved = approved_sources(cp, ex)
    eids = defaultdict(set)
    for row in episodes.iter_rows(named=True):
        eids[row["root_source_version_id"]].add(row["episode_id"])
    if events is not None:
        for row in events.iter_rows(named=True):
            eids[row["source_version_id"]].add(row["episode_id"])
    rows = []
    for message in mv.iter_rows(named=True):
        source = message["source_version_id"]
        if message["message_type"] != "message" or not (message["text"] or "").strip():
            continue
        if source not in extractions and source not in plans:
            continue  # Deduplicated/unextracted versions are not non-opening predictions.
        results = plans[source]
        chosen = [r for r in results if r["plan_id"] in selected]
        parsed = extractions[source]
        parser_results = [r for r in parsed if r["extractor"]["name"] == "parser"]
        llm_results = [r for r in parsed if r["extractor"]["name"] == "llm"]
        chosen_results = parser_results
        if plan_source == "llm" or (plan_source == "reconciled" and source in approved):
            chosen_results = llm_results
        elif plan_source == "reconciled":
            candidates = [dict(r, checks=dumps(r["checks"]), extractor_name="llm") for r in llm_results]
            chosen_results = llm_results + [r for r in parser_results if not action_overridden(r, candidates)]
        rows.append({"graph_version": graph_version, "source_version_id": source, "channel_id": message["channel_id"],
                     "message_time": message["message_date"], "text": message["text"],
                     "has_image": any(re.search(r"photo|image", k, re.I) for k in message["media_kinds"] or []),
                     "episode_ids": sorted(eids[source]), "plan_source": plan_source,
                     "is_open": any(r["kind"] == "entry_proposal" for r in chosen), "selected_results": chosen_results,
                     "canonical_plans": chosen, "llm_results": llm_results, "parser_results": parser_results})
    _atomic_text(layout.gold_dir / f"review_pool__{graph_version}.jsonl", "".join(dumps(r) + "\n" for r in rows))


def review_sample(layout, graph_version, output, *, sample, seed=0, channels=()):
    from .graph import resolve_alias
    from .sources import canonical_peer_id
    graph_version = resolve_alias(layout, graph_version)
    rows = list(read_jsonl(layout.gold_dir / f"review_pool__{graph_version}.jsonl"))
    selected = {canonical_peer_id(c, "channel") for c in channels}
    if selected:
        rows = [r for r in rows if r["channel_id"] in selected]
    # In a sample-only LLM build, a missing recording is not a non-opening vote.
    rows = [r for r in rows if r["selected_results"]]
    rows = stratified(rows, sample, seed, lambda r: (r["channel_id"], r["is_open"]))
    _atomic_text(output, "".join(dumps(r) + "\n" for r in rows))
    return {"exported": len(rows), "open": sum(r["is_open"] for r in rows), "graph_version": graph_version}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("export", "review-sample"):
        parser = sub.add_parser(name)
        parser.add_argument("--lake-root")
        parser.add_argument("--build-dir", help="flat layout used by api --out")
        parser.add_argument("--channel", type=int, action="append", default=[])
        parser.add_argument("--sample", type=int, required=name == "review-sample")
        parser.add_argument("--seed", type=int, default=0)
        parser.add_argument("--output", type=Path, required=True)
        if name == "review-sample":
            parser.add_argument("--graph-version", required=True)
        else:
            parser.add_argument("--exclude-prompts", type=Path, action="append", default=[])
    parser = sub.add_parser("run")
    parser.add_argument("--prompts", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--codex", help="default: $HOME/.local/lib/codex-launch; use a fake executable for tests")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-chars", type=int, default=12000)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--backoff", type=float, default=30, help="seconds before the 2nd attempt, doubling; 0 disables")
    parser = sub.add_parser("import")
    parser.add_argument("--responses", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.command == "run":
        report = run_batches(args.prompts, args.output_dir, executable=args.codex, batch_size=args.batch_size,
                             max_chars=args.max_chars, concurrency=args.concurrency, retries=args.retries, timeout=args.timeout,
                             backoff=args.backoff)
    elif args.command == "import":
        report = import_responses(args.responses, args.output)
    else:
        from .lake import Layout
        layout = Layout.flat(args.build_dir) if args.build_dir else Layout.from_root(args.lake_root)
        if args.command == "export":
            report = export_prompts(layout, args.output, channels=args.channel, sample=args.sample, seed=args.seed, exclude_prompts=args.exclude_prompts)
        else:
            report = review_sample(layout, args.graph_version, args.output, sample=args.sample, seed=args.seed, channels=args.channel)
    print(dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
