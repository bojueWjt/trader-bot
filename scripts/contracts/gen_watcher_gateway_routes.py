"""Generate committed watcher route modules from the contract YAML."""

import argparse
import sys

from watcher_gateway_routes_lib import EnvironmentError, load_source, render, validate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase-max", required=True, choices=("P0", "P1", "P2", "P3"))
    args = parser.parse_args()
    try:
        data, digest = load_source()
        rows = validate(data)
        for path, content in render(data, digest, args.phase_max).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        print(f"ROUTES_DIFF_EMPTY rows={rows} yaml_sha256={digest} phase_max={args.phase_max}")
        return 0
    except EnvironmentError as exc:
        print(f"ENVIRONMENT_ERROR {exc}")
        return 2
    except (ValueError, OSError) as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
