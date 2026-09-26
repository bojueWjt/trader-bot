"""Validate source invariants and byte exact committed route modules."""

import argparse
import json
import re
import sys

from watcher_gateway_routes_lib import CADDY, CADDY_SNIPPET, EnvironmentError, JAVASCRIPT, OUTPUTS, PYTHON, load_source, render, validate


def _code_payload(content):
    text = content.decode("utf-8")
    start = text.find("_TEXT = ")
    if start < 0:
        raise ValueError("code payload missing")
    encoded, _ = json.JSONDecoder().raw_decode(text[start + len("_TEXT = "):])
    return json.loads(encoded)


def _meta(path, content):
    text = content.decode("utf-8")
    if path in (CADDY, CADDY_SNIPPET):
        lines = text.splitlines()
        if len(lines) < 4:
            raise ValueError("caddy artifact missing headers")
        parsed = {}
        for line, key in zip(lines[:4], ("_generated_from", "_yaml_sha256", "_phase_max", "_format")):
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


def _independent_caddy(data, committed):
    paths = committed[CADDY].decode("ascii")
    snippet = committed[CADDY_SNIPPET].decode("ascii")
    if "*" in paths or "*" in snippet:
        raise ValueError("Caddy artifact contains *")
    if "# _format watcher-gateway-caddy-paths.v2\n" not in paths or "# _format watcher-gateway-caddy-snippet.v1\n" not in snippet:
        raise ValueError("Caddy format")
    lines = [line for line in paths.splitlines() if line and not line.startswith("#")]
    if not lines:
        raise ValueError("zero Caddy probes")
    snippet_lines = snippet.splitlines()
    observed = []
    for index, line in enumerate(snippet_lines):
        if line.startswith("\t\tpath_regexp ") and index + 1 < len(snippet_lines) and snippet_lines[index + 1].startswith("\t\tmethod "):
            observed.append((line[len("\t\tpath_regexp "):], snippet_lines[index + 1][len("\t\tmethod "):]))
    expected = []
    for line in lines:
        template, regex, *methods = line.split(" ")
        expected.append((regex, " ".join(methods)))
        example = re.sub(r"\{[^{}]+\}", "x", template)
        if re.fullmatch(regex, example) is None:
            raise ValueError("Caddy positive probe")
        rejects = [example + "/", example.upper()]
        if "{" in template:
            rejects.extend((re.sub(r"\{[^{}]+\}", "", template, count=1), re.sub(r"\{[^{}]+\}", "x/y", template, count=1)))
        else:
            rejects.append(example + "/x")
        if template.rsplit("/", 1)[-1].startswith("{"):
            accepts = [example + "\n", example + "#x"]
        else:
            accepts = []
            rejects.extend((example + "\n", example + "#x"))
        if any(re.fullmatch(regex, item) is not None for item in rejects) or any(re.fullmatch(regex, item) is None for item in accepts):
            raise ValueError("Caddy row probe")
    if observed != expected:
        raise ValueError("Caddy snippet rows")
    prefix = data["paths"]["caddy_external_prefix"] + data["paths"]["app_outer_prefix"]
    fallback = "^(?i:" + prefix + r")(?:[/\n]|$)"
    if snippet_lines[-7:] != ["\t@wgw_fallback {", "\t\tpath_regexp " + fallback, "\t}", "\thandle @wgw_fallback {", "\t\trespond 404", "\t}", "}"]:
        raise ValueError("Caddy fallback shape")
    good = [prefix, prefix + "/", prefix.upper() + "/login/start", prefix + "/login/start", prefix + "\n", prefix.upper() + "\n", prefix + "/status\n", prefix + "/status#x"]
    bad = [prefix + "x", data["paths"]["caddy_external_prefix"] + "/other", prefix + "x\n", prefix + "\r", prefix + "#x"]
    if any(re.search(fallback, item) is None for item in good) or any(re.search(fallback, item) is not None for item in bad):
        raise ValueError("Caddy fallback probe")


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
        if len(committed) != 4:
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
        saved = dict((path, content) for path, content, _ in committed)
        if not data["never_allowed"] or any(_code_payload(saved[path]).get("never_allowed") != data["never_allowed"] for path in (PYTHON, JAVASCRIPT)):
            raise ValueError("never_allowed generated payload mismatch")
        try:
            _independent_caddy(data, saved)
        except ValueError as exc:
            raise ValueError(f"DIFF {exc}") from exc
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
