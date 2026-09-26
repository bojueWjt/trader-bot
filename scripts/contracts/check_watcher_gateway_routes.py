"""Validate source invariants and byte exact committed route modules."""

import argparse
import ast
import sys

from watcher_gateway_routes_lib import EnvironmentError, OUTPUTS, load_source, render, validate


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
        source = OUTPUTS[0].read_text()
        tree = ast.parse(source)
        phase = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_TEXT" for t in node.targets):
                import json
                phase = json.loads(ast.literal_eval(node.value))["_meta"]["phase_max"]
        if phase is None:
            raise ValueError("generated Python payload missing")
        expected = render(data, digest, phase)
        mismatches = [str(path) for path, content in expected.items() if path.read_bytes() != content]
        if mismatches:
            for path in mismatches:
                print(f"DIFF {path}")
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
