"""Validate source invariants and byte exact committed route modules."""

import argparse
import json
import sys

from watcher_gateway_routes_lib import CADDY, EnvironmentError, OUTPUTS, load_source, render, validate


def _meta(path, content):
    text = content.decode("utf-8")
    if path == CADDY:
        lines = text.splitlines()
        if len(lines) < 3:
            raise ValueError("caddy artifact missing headers")
        parsed = {}
        for line, key in zip(lines[:3], ("_generated_from", "_yaml_sha256", "_phase_max")):
            prefix = f"# {key} "
            if not line.startswith(prefix):
                raise ValueError(f"caddy header {key}")
            parsed[key] = line[len(prefix):]
        return parsed["_yaml_sha256"], parsed["_phase_max"]
    start = text.find("_TEXT = ")
    if start < 0:
        raise ValueError(f"payload missing in {path}")
    payload_text, _ = json.JSONDecoder().raw_decode(text[start + len("_TEXT = "):])
    meta = json.loads(payload_text)["_meta"]
    return meta["yaml_sha256"], meta["phase_max"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    try:
        data, digest = load_source()
        rows = validate(data)
        if rows == 0:
            raise ValueError("zero routes compared")
        if args.self_check:
            print(f"ROUTES_DIFF_EMPTY rows={rows} yaml_sha256={digest} phase_max=self-check")
            return 0
        committed = []
        for path in OUTPUTS:
            content = path.read_bytes()
            committed.append((path, content, _meta(path, content)))
        if len(committed) != 3:
            raise ValueError("zero artifacts compared")
        python_digest, phase = committed[0][2]
        if phase != "P2":
            print(f"PHASE_MAX_NOT_P2 phase_max={phase}")
            return 1
        expected = render(data, digest, phase)
        if set(expected) != set(OUTPUTS):
            raise ValueError("generated outputs mismatch")
        path_lines = [line for line in expected[CADDY].decode("ascii").splitlines() if line and not line.startswith("#")]
        if phase == "P2" and not path_lines:
            raise ValueError("zero P2 caddy paths compared")
        mismatches = []
        metas = [item[2] for item in committed]
        if len(set(metas)) != 1 or metas[0] != (digest, phase) or python_digest != digest:
            mismatches.append("metadata")
        for path, content in expected.items():
            if path.read_bytes() != content:
                mismatches.append(str(path))
            if _meta(path, content) != (digest, phase):
                mismatches.append("metadata")
        unique = []
        for item in mismatches:
            if item not in unique:
                unique.append(item)
        if unique:
            for item in unique:
                print(f"DIFF {item}")
            return 1
        print(f"ROUTES_DIFF_EMPTY rows={rows} yaml_sha256={digest} phase_max={phase}")
        return 0
    except EnvironmentError as exc:
        print(f"ENVIRONMENT_ERROR {exc}")
        return 2
    except (ValueError, OSError, SyntaxError) as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
