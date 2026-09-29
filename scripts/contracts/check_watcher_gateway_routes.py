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


# Independent value pool for the "G1 => D" oracle (§9.14.4 item 1, WGW-1.0.3).
PARAM_VALUE_POOL = ("account-a", "A.b@c-1", "BTCUSDT", "-1001234567890", "1700000000000-1.png", "1")


def _group(name, body):
    return ["\t@" + name + " {", *body, "\t}", "\thandle @" + name + " {"]


def _snippet_blocks(snippet_lines):
    """Split the snippet body (after the 4 header lines) into top level blocks."""
    blocks = []
    current = None
    for line in snippet_lines[4:]:
        if current is None:
            match = re.fullmatch(r"\(([a-z_]+)\) \{", line)
            if match is None:
                raise ValueError("Caddy snippet top level line")
            current = (match.group(1), [])
        elif line == "}":
            blocks.append(current)
            current = None
        else:
            if not line.startswith("\t"):
                raise ValueError("Caddy snippet block line")
            current[1].append(line)
    if current is not None:
        raise ValueError("Caddy snippet unterminated block")
    return blocks


def _independent_caddy(data, committed):
    paths = committed[CADDY].decode("ascii")
    snippet = committed[CADDY_SNIPPET].decode("ascii")
    if "*" in paths or "*" in snippet:
        raise ValueError("Caddy artifact contains *")
    if "# _format watcher-gateway-caddy-paths.v2\n" not in paths or snippet.splitlines()[3:4] != ["# _format watcher-gateway-caddy-snippet.v2"]:
        raise ValueError("Caddy format")
    lines = [line for line in paths.splitlines() if line and not line.startswith("#")]
    if not lines:
        raise ValueError("zero Caddy probes")
    snippet_lines = snippet.splitlines()
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
    external = data["paths"]["caddy_external_prefix"]
    outer = data["paths"]["app_outer_prefix"]
    prefix = external + outer
    fallback = "^(?i:" + prefix + r")(?:[/\n]|$)"
    direct = "^(?i:" + outer + r")(?:[/\n%]|$)"
    if direct != r"^(?i:/v1/watcher)(?:[/\n%]|$)":
        raise ValueError("Caddy direct guard regex")
    fallback_group = ["\t@wgw_fallback {", "\t\tpath_regexp " + fallback, "\t}", "\thandle @wgw_fallback {", "\t\trespond 404", "\t}"]
    guard_group = ["\t@wgw_direct {", "\t\tpath_regexp " + direct, "\t}", "\thandle @wgw_direct {", "\t\trespond 404", "\t}"]
    blocks = _snippet_blocks(snippet_lines)
    if [name for name, _ in blocks] != ["watcher_gateway_routes", "watcher_gateway_direct_guard"]:
        raise ValueError("Caddy snippet names")
    routes_body, guard_body = blocks[0][1], blocks[1][1]
    if guard_body != guard_group:
        raise ValueError("Caddy direct guard snippet shape")
    if routes_body[-12:] != fallback_group + guard_group:
        raise ValueError("Caddy fallback shape")
    groups = routes_body[:-12]
    if len(groups) != 7 * len(lines):
        raise ValueError("Caddy snippet rows")
    observed = []
    for index in range(0, len(groups), 7):
        chunk = groups[index:index + 7]
        head = re.fullmatch(r"\t@(wgw_r_[a-z0-9_]+) \{", chunk[0])
        if head is None or head.group(1) in ("wgw_fallback", "wgw_direct"):
            raise ValueError("Caddy snippet rows")
        name = head.group(1)
        if not (chunk[1].startswith("\t\tpath_regexp ") and chunk[2].startswith("\t\tmethod ") and chunk[3:] == ["\t}", "\thandle @" + name + " {", "\t\timport watcher_gateway_upstream", "\t}"]):
            raise ValueError("Caddy snippet rows")
        observed.append((chunk[1][len("\t\tpath_regexp "):], chunk[2][len("\t\tmethod "):]))
    if observed != expected:
        raise ValueError("Caddy snippet rows")
    good = [prefix, prefix + "/", prefix.upper() + "/login/start", prefix + "/login/start", prefix + "\n", prefix.upper() + "\n", prefix + "/status\n", prefix + "/status#x"]
    bad = [prefix + "x", external + "/other", prefix + "x\n", prefix + "\r", prefix + "#x"]
    if any(re.search(fallback, item) is None for item in good) or any(re.search(fallback, item) is not None for item in bad):
        raise ValueError("Caddy fallback probe")
    direct_good = [outer, outer + "/", outer.upper() + "/status", outer + "/status", outer + "\n", outer.upper() + "\n", outer + "/status\n", outer + "%", outer + "%x", outer + "/media/1700000000000-1.png", outer + "/status#x"]
    direct_bad = [outer + "x", outer + "x\n", outer + "\r", outer + "#x", "/v1/other", "/v1/accounts", "/x" + outer[1:], prefix, prefix + "/status", "/" + outer + "/status"]
    if any(re.search(direct, item) is None for item in direct_good) or any(re.search(direct, item) is not None for item in direct_bad):
        raise ValueError("Caddy direct guard probe")
    if any(re.search(fallback, item) is not None for item in direct_good) or any(re.search(direct, item) is not None for item in (prefix, prefix + "/status")):
        raise ValueError("Caddy prefix regexes intersect")
    params = data["path_params"]
    compared = 0
    for line in lines:
        template = line.split(" ")[0]
        names = re.findall(r"\{([^{}]+)\}", template)
        candidates = [template]
        for name in names:
            pattern = params[name]["gateway_pattern"]
            values = [value for value in PARAM_VALUE_POOL if re.fullmatch(pattern, value)]
            if not values:
                raise ValueError(f"Caddy G1=>D pool empty for {name}")
            candidates = [item.replace("{" + name + "}", value, 1) for item in candidates for value in ("x", *values)]
        for candidate in candidates:
            if not candidate.startswith(external + "/"):
                raise ValueError("Caddy G1=>D external prefix")
            if re.search(direct, candidate[len(external):]) is None:
                raise ValueError("Caddy G1=>D guard miss")
            compared += 1
    if compared == 0:
        raise ValueError("zero G1=>D probes")


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
