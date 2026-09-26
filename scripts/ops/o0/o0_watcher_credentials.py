#!/usr/bin/env python3
"""O-0 watcher service credentials: generate, rotate, retire, check, apply.

DRAFT for the O-0 release (docs/agent-team/release/o0-runbook-credentials.md).

Rules this tool enforces (plan v0.6 §2.1, contract WGW-1.0.1 §9.2 / E-02 / E-15,
review wac-007 🟡-3):

* three identities: gateway, snapshot, browser; each has ``*_TOKEN`` and
  ``*_TOKEN_PREVIOUS`` on the watcher side;
* generated values use the alphabet ``[A-Za-z0-9_-]`` and are >= 32 bytes
  (``secrets.token_urlsafe(32)`` gives 43 characters), which is a strict
  subset of the contract format ``^[\\x21-\\x7E]{32,}$`` and is safe in
  Caddyfile, docker compose env_file and systemd EnvironmentFile;
* all configured watcher values are pairwise distinct;
* every holder value equals the watcher current value (or, while rotating,
  the watcher previous value);
* no watcher value equals any control-plane token catalog value
  (security/principal.py ``configured_token_values``); comparison is done on
  SHA-256 digests;
* the watcher env file carries no control-plane token names.

Values are NEVER printed, logged or placed on a command line. Output only
contains variable names, file names and PASS/FAIL lines.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
from pathlib import Path

IDENTITIES = {
    "gateway": "WATCHER_GATEWAY_TOKEN",
    "snapshot": "WATCHER_SNAPSHOT_TOKEN",
    "browser": "WATCHER_BROWSER_PROXY_TOKEN",
}
HOLDER_FILES = {
    # file name inside a credential set -> identities whose CURRENT value it holds
    "operator-query.env": ("gateway", "snapshot"),
    "caddy.env": ("browser",),
}
WATCHER_FILE = "watcher.env"
GENERATED_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,}$")
CONTRACT_PATTERN = re.compile(r"^[\x21-\x7E]{32,}$")
# security/permissions.py TOKEN_ENV_VARS + principal.py SIGNAL_TOKEN_ENV + other secrets
CONTROL_PLANE_SECRET_NAMES = (
    "RISK_ADMIN_TOKEN",
    "VIEWER_TOKEN",
    "REVIEWER_TOKEN",
    "SYSTEM_OBSERVER_TOKEN",
    "NAUTILUS_NODE_TOKEN",
    "SIGNAL_TOKEN_ACCOUNT_A",
    "SIGNAL_TOKEN_ACCOUNT_B",
    "SIGNAL_TOKEN_ACCOUNT_C",
    "SIGNAL_TOKEN_ACCOUNT_D",
    "CONTROL_PLANE_AUTH_SECRET",
)
NODE_AUTH_JSON = "NAUTILUS_NODE_AUTH_JSON"
ALLOWED_OUTPUT_ROOTS = ("/srv/trader-staging/",)
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CredentialError(Exception):
    pass


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines without any shell expansion."""
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialError(f"cannot read env file: {path.name}") from exc
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise CredentialError(f"{path.name}:{lineno}: not KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.match(key):
            raise CredentialError(f"{path.name}:{lineno}: invalid key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in result:
            raise CredentialError(f"{path.name}: duplicate key {key}")
        result[key] = value
    return result


def _configured(env: dict[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None or value == "":
        return None
    return value


def _write_env_file(path: Path, items: list[tuple[str, str]]) -> None:
    body = "".join(f"{k}={v}\n" for k, v in items)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)


def _require_output_dir(out_dir: Path, allow_any_dir: bool) -> None:
    resolved = str(out_dir.resolve()) + "/"
    if not allow_any_dir and not resolved.startswith(ALLOWED_OUTPUT_ROOTS):
        raise CredentialError(
            "output directory must be under /srv/trader-staging "
            "(use --allow-any-dir only for local self-tests)"
        )
    if out_dir.exists():
        raise CredentialError(f"output directory already exists: {out_dir}")


def _new_token(existing_digests: set[str]) -> str:
    for _ in range(16):
        value = secrets.token_urlsafe(32)
        if GENERATED_PATTERN.match(value) and _digest(value) not in existing_digests:
            return value
    raise CredentialError("could not generate a distinct token")


def _write_set(out_dir: Path, current: dict[str, str], previous: dict[str, str]) -> None:
    out_dir.mkdir(mode=0o700, parents=False)
    watcher_items = []
    for ident, name in IDENTITIES.items():
        watcher_items.append((name, current[ident]))
        watcher_items.append((f"{name}_PREVIOUS", previous.get(ident, "")))
    _write_env_file(out_dir / WATCHER_FILE, watcher_items)
    for file_name, idents in HOLDER_FILES.items():
        _write_env_file(
            out_dir / file_name,
            [(IDENTITIES[i], current[i]) for i in idents],
        )


def _load_set(set_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    env = parse_env_file(set_dir / WATCHER_FILE)
    current: dict[str, str] = {}
    previous: dict[str, str] = {}
    for ident, name in IDENTITIES.items():
        value = _configured(env, name)
        if value is None:
            raise CredentialError(f"{WATCHER_FILE}: {name} missing")
        current[ident] = value
        prev = _configured(env, f"{name}_PREVIOUS")
        if prev is not None:
            previous[ident] = prev
    return current, previous


def cmd_generate(args: argparse.Namespace) -> int:
    _require_output_dir(args.out_dir, args.allow_any_dir)
    seen: set[str] = set()
    current = {}
    for ident in IDENTITIES:
        value = _new_token(seen)
        seen.add(_digest(value))
        current[ident] = value
    _write_set(args.out_dir, current, {})
    print(f"GENERATED set={args.out_dir.name} files={WATCHER_FILE},{','.join(HOLDER_FILES)}")
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    _require_output_dir(args.out_dir, args.allow_any_dir)
    current, previous = _load_set(args.from_dir)
    if previous:
        raise CredentialError(
            "source set still has *_PREVIOUS values; retire them before a new rotation"
        )
    seen = {_digest(v) for v in current.values()}
    new_current = dict(current)
    new_previous = {args.identity: current[args.identity]}
    new_current[args.identity] = _new_token(seen)
    _write_set(args.out_dir, new_current, new_previous)
    print(
        f"ROTATED identity={args.identity} set={args.out_dir.name} "
        f"previous={IDENTITIES[args.identity]}_PREVIOUS"
    )
    return 0


def cmd_retire(args: argparse.Namespace) -> int:
    _require_output_dir(args.out_dir, args.allow_any_dir)
    current, previous = _load_set(args.from_dir)
    if not previous:
        raise CredentialError("source set has no *_PREVIOUS value to retire")
    _write_set(args.out_dir, current, {})
    print(f"RETIRED previous={','.join(IDENTITIES[i] + '_PREVIOUS' for i in previous)} set={args.out_dir.name}")
    return 0


def _catalog_values(paths: list[Path]) -> tuple[list[str], list[str]]:
    values: list[str] = []
    names: list[str] = []
    for path in paths:
        env = parse_env_file(path)
        for name in CONTROL_PLANE_SECRET_NAMES:
            value = _configured(env, name)
            if value is not None:
                values.append(value.strip())
                names.append(f"{path.name}:{name}")
        raw = _configured(env, NODE_AUTH_JSON)
        if raw is not None:
            try:
                bindings = json.loads(raw)
                if not isinstance(bindings, dict):
                    raise ValueError
                for node, binding in bindings.items():
                    token = str((binding or {}).get("token", "") or "").strip()
                    if token:
                        values.append(token)
                        names.append(f"{path.name}:{NODE_AUTH_JSON}[{node}]")
            except (ValueError, TypeError, AttributeError) as exc:
                raise CredentialError(f"{path.name}: {NODE_AUTH_JSON} is not a valid object") from exc
    return values, names


def cmd_check(args: argparse.Namespace) -> int:
    failures: list[str] = []
    passes: list[str] = []
    watcher_env = parse_env_file(args.watcher_env)

    for name in CONTROL_PLANE_SECRET_NAMES + (NODE_AUTH_JSON,):
        if name in watcher_env:
            failures.append(f"watcher env must not carry control-plane secret {name}")

    configured: dict[str, str] = {}
    for ident, name in IDENTITIES.items():
        for var in (name, f"{name}_PREVIOUS"):
            value = _configured(watcher_env, var)
            if value is None:
                if var == name:
                    failures.append(f"{var} not configured (watcher refuses to start)")
                continue
            configured[var] = value
            if not CONTRACT_PATTERN.match(value):
                failures.append(f"{var} violates contract format ^[\\x21-\\x7E]{{32,}}$")
            elif not GENERATED_PATTERN.match(value):
                failures.append(f"{var} violates O-0 alphabet [A-Za-z0-9_-]{{32,}}")
            else:
                passes.append(f"{var} format")
    digests: dict[str, list[str]] = {}
    for var, value in configured.items():
        digests.setdefault(_digest(value), []).append(var)
    for names in digests.values():
        if len(names) > 1:
            failures.append("watcher values not distinct: " + ",".join(sorted(names)))
    if configured and all(len(v) == 1 for v in digests.values()):
        passes.append(f"watcher values pairwise distinct ({len(configured)})")

    rotating = {
        ident for ident, name in IDENTITIES.items() if f"{name}_PREVIOUS" in configured
    }
    for holder_path in args.holder_env or []:
        holder = parse_env_file(holder_path)
        idents = HOLDER_FILES.get(holder_path.name)
        if idents is None:
            idents = tuple(i for i, n in IDENTITIES.items() if n in holder)
        for ident in idents:
            name = IDENTITIES[ident]
            value = _configured(holder, name)
            if value is None:
                failures.append(f"{holder_path.name}: {name} not configured")
                continue
            cur = configured.get(name)
            prev = configured.get(f"{name}_PREVIOUS")
            if cur is not None and _digest(value) == _digest(cur):
                passes.append(f"{holder_path.name}: {name} == watcher current")
            elif prev is not None and _digest(value) == _digest(prev):
                if args.allow_holder_on_previous:
                    passes.append(f"{holder_path.name}: {name} == watcher previous (rotation step 1)")
                else:
                    failures.append(
                        f"{holder_path.name}: {name} still equals watcher previous "
                        "(pass --allow-holder-on-previous only during rotation step 1)"
                    )
            else:
                failures.append(f"{holder_path.name}: {name} matches no configured watcher value")
        for other in IDENTITIES.values():
            if other in holder and IDENTITIES_BY_NAME[other] not in idents:
                failures.append(f"{holder_path.name}: holds {other} it does not need")

    if args.catalog_env:
        catalog, catalog_names = _catalog_values(args.catalog_env)
        if not catalog:
            failures.append("control-plane catalog is empty: cross-service check compared 0 values")
        catalog_digests = {_digest(v) for v in catalog}
        for var, value in configured.items():
            if _digest(value) in catalog_digests:
                failures.append(f"{var} equals a control-plane catalog value")
        if catalog and not any("catalog value" in f for f in failures):
            passes.append(
                f"cross-service distinct: {len(configured)} watcher values vs "
                f"{len(catalog)} catalog values"
            )
    elif args.require_catalog:
        failures.append("--catalog-env required for release check (E-15)")

    for line in passes:
        print(f"PASS {line}")
    for line in failures:
        print(f"FAIL {line}")
    if rotating:
        print("INFO rotating identities: " + ",".join(sorted(rotating)))
    if failures:
        print(f"CREDENTIAL_CHECK_FAILED failures={len(failures)}")
        return 1
    if not passes:
        print("CREDENTIAL_CHECK_FAILED compared=0")
        return 1
    print(f"CREDENTIAL_CHECK_OK checks={len(passes)}")
    return 0


IDENTITIES_BY_NAME = {name: ident for ident, name in IDENTITIES.items()}


def cmd_apply(args: argparse.Namespace) -> int:
    """Merge KEY=VALUE lines of a fragment into a target env file.

    Dry-run (default) prints key names and add/replace only. Execute writes a
    0600 backup first, then replaces the target atomically keeping mode/owner.
    """
    fragment = parse_env_file(args.fragment)
    if not fragment:
        raise CredentialError("fragment is empty")
    target_lines: list[str] = []
    existing: dict[str, int] = {}
    if args.target.exists():
        target_lines = args.target.read_text(encoding="utf-8").splitlines()
        for index, raw in enumerate(target_lines):
            line = raw.strip()
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if line and not line.startswith("#") and "=" in line:
                key = line.split("=", 1)[0].strip()
                if key in existing:
                    raise CredentialError(f"target has duplicate key {key}")
                existing[key] = index
    elif not args.create:
        raise CredentialError(f"target does not exist: {args.target} (use --create)")
    actions = []
    for key, value in fragment.items():
        if args.remove:
            if key in existing:
                actions.append(("remove", key))
            continue
        actions.append(("replace" if key in existing else "add", key))
    for action, key in actions:
        print(f"{'APPLY' if args.execute else 'PLAN'} {action} {key} -> {args.target}")
    if not args.execute:
        return 0
    if args.backup_dir is None:
        raise CredentialError("--backup-dir is required with --execute")
    args.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.target.exists():
        backup = args.backup_dir / (args.target.name + ".bak")
        if backup.exists():
            raise CredentialError(f"backup already exists: {backup}")
        shutil.copy2(args.target, backup)
        os.chmod(backup, 0o600)
        print(f"BACKUP {args.target} -> {backup}")
    new_lines = list(target_lines)
    removed = set()
    for action, key in actions:
        if action == "replace":
            new_lines[existing[key]] = f"{key}={fragment[key]}"
        elif action == "add":
            new_lines.append(f"{key}={fragment[key]}")
        elif action == "remove":
            removed.add(existing[key])
    if removed:
        new_lines = [line for i, line in enumerate(new_lines) if i not in removed]
    mode = 0o600
    uid = gid = None
    if args.target.exists():
        st = args.target.stat()
        mode = stat.S_IMODE(st.st_mode)
        uid, gid = st.st_uid, st.st_gid
    fd, tmp = tempfile.mkstemp(prefix=".o0-", dir=str(args.target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(new_lines) + "\n")
        os.chmod(tmp, mode)
        if uid is not None and os.geteuid() == 0:
            os.chown(tmp, uid, gid)
        os.replace(tmp, args.target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"APPLIED keys={len(actions)} target={args.target}")
    return 0


def cmd_selftest(_args: argparse.Namespace) -> int:
    import contextlib
    import io

    base = Path(tempfile.mkdtemp(prefix="o0-cred-selftest-"))
    captured = io.StringIO()
    try:
        def run(argv: list[str]) -> int:
            with contextlib.redirect_stdout(captured):
                return main(argv)

        set1 = base / "set1"
        assert run(["generate", "--out-dir", str(set1), "--allow-any-dir"]) == 0
        for name in (WATCHER_FILE, *HOLDER_FILES):
            assert stat.S_IMODE((set1 / name).stat().st_mode) == 0o600, name
        catalog = base / "operator-query-secrets.env"
        catalog.write_text(
            "RISK_ADMIN_TOKEN=" + secrets.token_hex(24) + "\n"
            "SYSTEM_OBSERVER_TOKEN=" + secrets.token_hex(24) + "\n"
            "NAUTILUS_NODE_AUTH_JSON='{\"node-a\": {\"token\": \"" + secrets.token_hex(24) + "\"}}'\n",
            encoding="utf-8",
        )
        steady = [
            "check", "--watcher-env", str(set1 / WATCHER_FILE),
            "--holder-env", str(set1 / "operator-query.env"),
            "--holder-env", str(set1 / "caddy.env"),
            "--catalog-env", str(catalog), "--require-catalog",
        ]
        assert run(steady) == 0
        # rotation of gateway: step 1 holder still on old value -> needs flag
        set2 = base / "set2"
        assert run(["rotate", "--identity", "gateway", "--from-dir", str(set1), "--out-dir", str(set2), "--allow-any-dir"]) == 0
        step1 = [
            "check", "--watcher-env", str(set2 / WATCHER_FILE),
            "--holder-env", str(set1 / "operator-query.env"),
            "--catalog-env", str(catalog), "--require-catalog",
        ]
        assert run(step1) == 1
        assert run(step1 + ["--allow-holder-on-previous"]) == 0
        step2 = [
            "check", "--watcher-env", str(set2 / WATCHER_FILE),
            "--holder-env", str(set2 / "operator-query.env"),
            "--catalog-env", str(catalog), "--require-catalog",
        ]
        assert run(step2) == 0
        set3 = base / "set3"
        assert run(["retire", "--from-dir", str(set2), "--out-dir", str(set3), "--allow-any-dir"]) == 0
        step4 = [
            "check", "--watcher-env", str(set3 / WATCHER_FILE),
            "--holder-env", str(set1 / "operator-query.env"),
        ]
        assert run(step4) == 1, "retired old gateway value must be rejected"
        # collision with control-plane catalog must fail
        leak = base / "leak.env"
        watcher_vals = parse_env_file(set1 / WATCHER_FILE)
        leak.write_text("VIEWER_TOKEN=" + watcher_vals["WATCHER_GATEWAY_TOKEN"] + "\n", encoding="utf-8")
        assert run(["check", "--watcher-env", str(set1 / WATCHER_FILE), "--catalog-env", str(leak)]) == 1
        # duplicate values and short / bad-alphabet values must fail
        dup = base / "dup.env"
        dup_value = secrets.token_urlsafe(32)
        dup.write_text(
            f"WATCHER_GATEWAY_TOKEN={dup_value}\nWATCHER_SNAPSHOT_TOKEN={dup_value}\n"
            f"WATCHER_BROWSER_PROXY_TOKEN={secrets.token_urlsafe(32)}\n",
            encoding="utf-8",
        )
        assert run(["check", "--watcher-env", str(dup)]) == 1
        bad = base / "bad.env"
        bad.write_text(
            "WATCHER_GATEWAY_TOKEN=short\n"
            "WATCHER_SNAPSHOT_TOKEN=" + "{" + secrets.token_hex(20) + "}\n"
            "WATCHER_BROWSER_PROXY_TOKEN=" + secrets.token_urlsafe(32) + "\n"
            "RISK_ADMIN_TOKEN=" + secrets.token_hex(20) + "\n",
            encoding="utf-8",
        )
        assert run(["check", "--watcher-env", str(bad)]) == 1
        # empty catalog is "uncomparable", not "distinct"
        empty = base / "empty.env"
        empty.write_text("# nothing\n", encoding="utf-8")
        assert run(["check", "--watcher-env", str(set1 / WATCHER_FILE), "--catalog-env", str(empty)]) == 1
        # apply: plan mode does not touch the file; execute keeps other lines
        target = base / "v3.env"
        target.write_text("WATCHER_BASIC_AUTH_HASH=$2a$14$placeholderplaceholderplaceholderplaceholderplace\n", encoding="utf-8")
        os.chmod(target, 0o640)
        before = target.read_text(encoding="utf-8")
        assert run(["apply", "--fragment", str(set1 / "caddy.env"), "--target", str(target)]) == 0
        assert target.read_text(encoding="utf-8") == before
        assert run(["apply", "--fragment", str(set1 / "caddy.env"), "--target", str(target), "--execute", "--backup-dir", str(base / "bk")]) == 0
        after = parse_env_file(target)
        assert after["WATCHER_BASIC_AUTH_HASH"].startswith("$2a$14$")
        assert "WATCHER_BROWSER_PROXY_TOKEN" in after
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
        assert (base / "bk" / "v3.env.bak").read_text(encoding="utf-8") == before
        assert run(["apply", "--fragment", str(set1 / "caddy.env"), "--target", str(target), "--remove", "--execute", "--backup-dir", str(base / "bk2")]) == 0
        assert "WATCHER_BROWSER_PROXY_TOKEN" not in parse_env_file(target)
        # the output must never contain a generated value
        out = captured.getvalue()
        secrets_seen = []
        for directory in (set1, set2, set3):
            for name in (WATCHER_FILE, *HOLDER_FILES):
                secrets_seen.extend(v for v in parse_env_file(directory / name).values() if v)
        leaked = [v for v in secrets_seen if v in out]
        assert not leaked, "a credential value appeared in tool output"
        print(f"SELFTEST_OK scenarios=13 values_checked_for_leak={len(secrets_seen)} output_lines={len(out.splitlines())}")
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="create a new credential set (no PREVIOUS)")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--allow-any-dir", action="store_true")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("rotate", help="new current for one identity; old current becomes PREVIOUS")
    p.add_argument("--identity", choices=sorted(IDENTITIES), required=True)
    p.add_argument("--from-dir", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--allow-any-dir", action="store_true")
    p.set_defaults(func=cmd_rotate)

    p = sub.add_parser("retire", help="clear all *_PREVIOUS values")
    p.add_argument("--from-dir", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--allow-any-dir", action="store_true")
    p.set_defaults(func=cmd_retire)

    p = sub.add_parser("check", help="validate format, distinctness, holders and catalog")
    p.add_argument("--watcher-env", type=Path, required=True)
    p.add_argument("--holder-env", type=Path, action="append")
    p.add_argument("--catalog-env", type=Path, action="append")
    p.add_argument("--require-catalog", action="store_true")
    p.add_argument("--allow-holder-on-previous", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("apply", help="merge fragment keys into an env file (plan by default)")
    p.add_argument("--fragment", type=Path, required=True)
    p.add_argument("--target", type=Path, required=True)
    p.add_argument("--backup-dir", type=Path)
    p.add_argument("--create", action="store_true")
    p.add_argument("--remove", action="store_true", help="remove the fragment's keys instead")
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("selftest", help="run local self-tests with fake values")
    p.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CredentialError as exc:
        print(f"ERROR {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
