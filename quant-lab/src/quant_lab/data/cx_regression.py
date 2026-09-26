"""Local-only pilot regression. Never loads real texts into repository fixtures.

prepare creates fresh v2 prompts from the review's source JSONL (no gold labels).
check joins a new responses.jsonl by those prompt keys and reports message-level
pilot denominators. Semantic gold shapes that cannot be compared exactly remain
pending, count as incorrect in gate metrics, and may be independently adjudicated.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

from . import cx_batch as cx, cx_v2
from .extract import canonical_symbol
from .llm import record_key

DEFAULT_TRUTH = Path('/private/tmp/claude-501/-Users-balen-projects-trader-bot/0f23fe43-9647-49dc-9185-0f7c3826c2ff/scratchpad/cx-pilot/review-result.json')
FIELDS = ("symbol", "side", "entry", "stop", "tps")


def load(path):
    return json.loads(Path(path).read_text(), parse_float=Decimal)


def external(path):
    repo = Path(__file__).resolve().parents[4]
    if Path(path).resolve().is_relative_to(repo):
        raise ValueError("private_review_data_must_stay_outside_repository")
    return Path(path)


def prepare(truth_path, output):
    truth = load(truth_path)
    source = Path(truth['source_path'])
    inputs = {r['item_id']: r for r in cx.read_jsonl(source)}
    rows = []
    for gold in truth['items']:
        row = inputs[gold['item_id']]
        system, user = cx_v2.build_prompt(row['text'], channel_name=row['channel'], message_date=row['message_time'],
                                         previous_text=row.get('previous_text'))
        rows.append(dict(item_id=row['item_id'], channel_name=row['channel'], message_time=row['message_time'],
                         source_version_id=row['item_id'], text=row['text'], previous_text=row.get('previous_text'),
                         system=system, user=user, schema_name=cx_v2.SCHEMA_NAME,
                         key=record_key(system, user, cx_v2.SCHEMA_NAME)))
    cx._atomic_text(external(output), ''.join(cx.dumps(r) + '\n' for r in rows))
    return dict(exported=len(rows), contains_gold=False, blind_test=False)


def decimal(value):
    return str(Decimal(str(value)).normalize())


def atom(value):
    return decimal(value['value']) if value else None


def symbol(value):
    # Same canonicalizer the pipeline applies before registry lookup ($ENA, #UNI/USDT, near, 大饼).
    return canonical_symbol(value)


ALLOWED_GOLD_ENTRY = {'lo', 'hi', 'kind', 'levels', 'raw', 'unit_unspecified', 'raw_ambiguous'}


def entry_shape(value, *, predicted=False):
    """(priced anchors, market_leg); market_leg=None means the gold label does not say.

    Anchors are prices (or a two-sided zone) regardless of whether the price is a quoted CMP or a limit:
    gold levels like ["104", "95"] for "CMP(104) + DCA 95" cannot say which one is the CMP, so the CMP
    distinction is carried only by market_leg, which is compared wherever gold states it."""
    if value is None:
        return frozenset(), False
    if predicted:
        kind = value['kind']
        if kind == 'ladder':
            anchors = {('px', atom(p.get('price'))) for p in value['levels'] if not (p['kind'] == 'market_ref' and p.get('price') is None)}
            return frozenset(anchors), any(p['kind'] == 'market_ref' for p in value['levels'])
        if kind == 'zone':
            lo, hi = atom(value.get('lo')), atom(value.get('hi'))
            if lo is not None and hi is not None:
                return frozenset({('zone', *sorted([lo, hi], key=Decimal))}), False
            return frozenset({('px', lo if lo is not None else hi)}), False
        price = atom(value.get('price'))
        if kind == 'market_ref':
            return frozenset({('px', price)} if price is not None else set()), True
        return frozenset({('px', price)}), False
    if set(value) - ALLOWED_GOLD_ENTRY:
        raise ValueError('entry_semantics_require_review')
    if 'levels' in value:
        return frozenset(('px', decimal(v)) for v in value['levels']), None
    lo, hi = value.get('lo'), value.get('hi')
    kind = value.get('kind', 'limit')
    if kind == 'market_ref':
        if lo is None and hi is None:
            return frozenset(), True
        if lo != hi:
            raise ValueError('market_range_requires_review')
        return frozenset({('px', decimal(lo))}), True
    if lo is None and hi is None:
        return frozenset(), None
    if lo is None or hi is None:
        return frozenset({('px', decimal(lo if lo is not None else hi))}), None
    if kind == 'zone' or lo != hi:
        return frozenset({('zone', *sorted([decimal(lo), decimal(hi)], key=Decimal))}), None
    return frozenset({('px', decimal(lo))}), None


def signature(field, value, *, predicted=False):
    """Exact comparable shapes; unsupported semantics raise instead of passing."""
    if field in ('symbol', 'side'):
        vals = value if isinstance(value, list) else [value]
        return sorted({str(symbol(v) if field == 'symbol' else v) for v in vals})
    if field == 'stop':
        if value is None:
            return None
        if predicted:
            if value['kind'] == 'condition':
                return ('condition', condition_numbers(value.get('condition')))
            return atom(value.get('price'))
        if isinstance(value, dict):
            if set(value) == {'condition'}:
                return ('condition', condition_numbers(value['condition']))
            raise ValueError('stop_semantics_require_review')
        return decimal(value)
    if field == 'tps':
        if not value:
            return []
        if predicted:
            return sorted({(v['kind'], atom(v['value'])) for v in value})
        if isinstance(value, dict):
            if set(value) == {'relative_percent'}:
                return sorted({('percent', decimal(v)) for v in value['relative_percent']})
            raise ValueError('tp_semantics_require_review')
        return sorted({('price', decimal(v)) for v in value})
    raise ValueError('unknown_field')


def condition_numbers(text):
    """A condition stop is judged by the levels it cites, not by exact wording or the model's extra price field."""
    if not text:
        return frozenset()
    norm = unicodedata.normalize('NFKC', text)
    return frozenset(str(Decimal(n.replace(',', '')).normalize()) for n in re.findall(r'\d[\d,]*(?:\.\d+)?', norm))


def _prices(anchors):
    """Zone endpoints count as prices: "CMP 85500 到 84000" is written as a zone by some labellers and as a
    CMP+limit ladder by the model; both name the same two prices, and the CMP leg is judged separately."""
    out = set()
    for a in anchors:
        out.update(('px', v) for v in a[1:])
    return frozenset(out)


def _merge_entries(actions):
    """One idea split into several now-open actions for the same symbol (e.g. 10.8万 and 10.6万) is judged
    on the union of its prices and whether any leg is at market."""
    groups = {}
    for a in actions:
        groups.setdefault(symbol(a.get('symbol_raw')), []).append(entry_shape(a.get('entry'), predicted=True))
    return [(frozenset().union(*(g[0] for g in shapes)), any(g[1] for g in shapes)) for shapes in groups.values()]


def _leg_for(gold, name, identity):
    leg = gold.get('entry_market_leg')
    if isinstance(leg, dict) and 'by_symbol' in leg and identity == 'symbol_raw':
        return {symbol(k): v for k, v in leg['by_symbol'].items()}.get(symbol(name))
    return leg


def compare_field(field, gold, actions):
    """(correct, pending). Entry also reports whether the market leg was actually evaluated via gold['_leg_seen']."""
    if not actions or all(a['op'] == 'undecidable' for a in actions):
        return False, None
    expected = gold.get(field)
    key = 'symbol_raw' if field == 'symbol' else field
    if field in ('symbol', 'side'):
        return signature(field, expected) == signature(field, [a.get(key) for a in actions]), None
    if isinstance(expected, dict):
        for grouping, identity in (('by_symbol', 'symbol_raw'), ('by_side', 'side')):
            if grouping in expected:
                named = expected[grouping]
                actual = {symbol(a.get(identity)) if identity == 'symbol_raw' else a.get(identity) for a in actions}
                names = {symbol(k) if identity == 'symbol_raw' else k for k in named}
                if actual != names:
                    return False, None
                outcomes = []
                for name, value in named.items():
                    members = [a for a in actions if (symbol(a.get(identity)) == symbol(name) if identity == 'symbol_raw' else a.get(identity) == name)]
                    sub = {field: value, 'entry_market_leg': _leg_for(gold, name, identity)}
                    outcomes.append(compare_field(field, sub, members))
                    gold.setdefault('_leg_seen', []).extend(sub.get('_leg_seen', []))
                pending = next((p for _, p in outcomes if p), None)
                return all(ok for ok, _ in outcomes), pending
    try:
        if field == 'entry':
            anchors, leg = entry_shape(expected)
            explicit = gold.get('entry_market_leg')
            if isinstance(explicit, bool):
                leg = explicit
            got = _merge_entries(actions)
            gold.setdefault('_leg_seen', []).append(leg is not None)
            # A shared gold value applies to every action unless explicitly grouped.
            return all(_prices(g[0]) == _prices(anchors) and (leg is None or g[1] == leg) for g in got), None
        want = signature(field, expected)
        got = [signature(field, a.get(key), predicted=True) for a in actions]
        return all(v == want for v in got), None
    except (ValueError, TypeError, KeyError):
        return False, 'semantic_comparison_requires_independent_review'


def ratio(n, d, *, minimum=None, maximum=None):
    rate = n / d if d else None
    interval = None
    if d:
        z = 1.959963984540054
        center = (rate + z*z/(2*d)) / (1 + z*z/d)
        half = z * math.sqrt(rate*(1-rate)/d + z*z/(4*d*d)) / (1 + z*z/d)
        interval = [max(0, center-half), min(1, center+half)]
    passed = None if rate is None else (rate >= minimum if minimum is not None else rate <= maximum if maximum is not None else None)
    return dict(numerator=n, denominator=d, rate=rate, wilson95=interval, passes=passed)


def summarize(rows):
    opens = [r for r in rows if r['truth_open']]
    predicted = [r for r in rows if r['predicted_open'] and r['op_evaluable']]
    metrics = dict(open_false_discovery=ratio(sum(not r['truth_open'] for r in predicted), len(predicted), maximum=.05),
                   open_miss=ratio(sum(not r['predicted_open'] for r in opens), len(opens), maximum=.05))
    for f in FIELDS:
        scored = [r for r in opens if f in r['fields']]
        metrics[f] = ratio(sum(r['fields'][f]['correct'] for r in scored), len(scored), minimum=.95)
    for name in ('evidence_discarded', 'whole_message_discarded', 'model_uncertain', 'field_evidence_failed_messages'):
        metrics[name] = ratio(sum(bool(r[name]) for r in rows), len(rows), maximum=.05 if name == 'evidence_discarded' else None)
    evaluable = [r for r in rows if r['op_evaluable']]
    metrics['op_agreement'] = ratio(sum(r['op_matches'] for r in evaluable), len(evaluable))
    metrics['raw_op_agreement'] = ratio(sum(r['raw_op_matches'] for r in evaluable), len(evaluable))
    abstentions = [r for r in rows if r['whole_message_discarded'] and r['op_evaluable']]
    metrics['unjustified_discard'] = ratio(sum(r['truth_op'] != 'undecidable' for r in abstentions), len(abstentions))
    gates = [m['passes'] for m in metrics.values() if m['passes'] is not None]
    return dict(metrics=metrics, field_evidence_failed=sum(r['field_evidence_failed'] for r in rows),
                pending_fields=sum(bool(v['pending']) for r in rows for v in r['fields'].values()),
                entry_market_leg_unevaluated=sum(not r['fields']['entry'].get('market_leg_evaluated', True) for r in opens if 'entry' in r['fields']),
                passes=all(gates) and all(metrics[k]['passes'] is True for k in ('open_false_discovery', 'open_miss', *FIELDS, 'evidence_discarded')))


def check(truth_path, prompts_path, responses_path, *, adjudications=None):
    truth = load(truth_path)
    prompts = list(cx.read_jsonl(prompts_path))
    by_id = {p['item_id']: p for p in prompts}
    if len(by_id) != len(prompts) or set(by_id) != {r['item_id'] for r in truth['items']}:
        raise ValueError('prompt_truth_population_mismatch')
    source = {r['item_id']: r for r in cx.read_jsonl(truth['source_path'])}
    for p in prompts:
        original = source[p['item_id']]
        expected_system, expected_user = cx_v2.build_prompt(original['text'], channel_name=original['channel'],
                              message_date=original['message_time'], previous_text=original.get('previous_text'))
        if p['key'] != record_key(expected_system, expected_user, cx_v2.SCHEMA_NAME) or p['text'] != original['text']:
            raise ValueError('prompt_source_mismatch')
    responses = {}
    keys = {p['key'] for p in prompts}
    for r in cx.read_jsonl(responses_path):
        if r['key'] in responses or r['key'] not in keys:
            raise ValueError('duplicate_or_unknown_response_key')
        responses[r['key']] = r
    overrides = adjudications or {}
    rows = []
    for gold in truth['items']:
        identity = gold['item_id']
        prompt = by_id[identity]
        response = responses.get(prompt['key'], {})
        payload = response.get('response')
        actions, stats = [], {}
        invalid = False
        if payload is not None:
            try:
                clean = cx_v2.validate_response(payload, prompt['text'])
                actions, stats = clean['actions'], clean['stats']
            except (ValueError, TypeError, KeyError):
                invalid = True
        note = response.get('abstain', {}).get('note', '')
        op_eval = gold.get('comparison', {}).get('op_evaluable', 'op' not in gold.get('image_affected_fields', []))
        truth_open = gold['truth_op'] == 'open' and op_eval
        now_opens = [a for a in actions if a['op'] == 'open' and a['time_ref'] == 'now']
        predicted_open = bool(now_opens)
        discarded = not response or 'abstain' in response or invalid or bool(stats.get('whole_message_discarded'))
        model_uncertain = bool(stats.get('model_uncertain')) or note == 'model_undecidable'
        op = 'open' if predicted_open else (actions[0]['op'] if len(actions) == 1 else 'chatter' if payload and not actions else None)
        raw_op = op
        if model_uncertain and not actions:
            op = 'undecidable'
        scored = {}
        if truth_open:
            for field in FIELDS:
                mask = gold.get('comparison', {}).get('fields', {}).get(field, {})
                if mask.get('evaluable') is False or field in gold.get('image_affected_fields', []):
                    continue
                # Gold fields describe the new trade; a sibling analysis/close action must not dilute them.
                probe = dict(gold)
                correct, pending = compare_field(field, probe, now_opens or actions)
                override = overrides.get(identity, {}).get(field)
                if override is not None:
                    if type(override.get('correct')) is not bool or not override.get('reason'):
                        raise ValueError('adjudication_requires_boolean_and_reason')
                    # Never override discard or a rejected/missing numeric field as correct.
                    if override['correct'] and (discarded or any(i['reason'].startswith('evidence_rejected:') for a in actions for i in a['field_issues'])):
                        raise ValueError('adjudication_cannot_approve_discard_or_rejected_evidence')
                    correct, pending = override['correct'], None
                scored[field] = dict(correct=correct, pending=pending)
                if field == 'entry':
                    # Uncomparable is not equal: say when gold never stated whether a CMP leg exists.
                    scored[field]['market_leg_evaluated'] = all(probe.get('_leg_seen') or [False])
        rows.append(dict(item_id=identity, channel=gold['channel'], truth_open=truth_open, predicted_open=predicted_open,
                         truth_op=gold['truth_op'], op_evaluable=op_eval, op_matches=op == gold['truth_op'],
                         raw_op_matches=raw_op == gold['truth_op'], fields=scored,
                         whole_message_discarded=discarded, model_uncertain=model_uncertain,
                         evidence_discarded=discarded and note.startswith('evidence_rejected'),
                         field_evidence_failed=stats.get('field_evidence_failed', 0),
                         field_evidence_failed_messages=bool(stats.get('field_evidence_failed'))))
    channels = defaultdict(list)
    for row in rows:
        channels[row['channel']].append(row)
    return dict(schema_version=2, blind_test=False, population=len(rows), missing_responses=sum(p['key'] not in responses for p in prompts),
                inputs_sha256={name: hashlib.sha256(Path(path).read_bytes()).hexdigest() for name, path in
                               [('truth', truth_path), ('prompts', prompts_path), ('responses', responses_path)]},
                overall=summarize(rows), channels={k: summarize(v) for k, v in channels.items()}, items=rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'check'):
        p = sub.add_parser(name)
        p.add_argument('--truth', type=Path, default=DEFAULT_TRUTH)
        p.add_argument('--output', type=Path, required=True)
        if name == 'check':
            p.add_argument('--prompts', type=Path, required=True)
            p.add_argument('--responses', type=Path, required=True)
            p.add_argument('--adjudications', type=Path)
    args = parser.parse_args(argv)
    external(args.truth)
    external(args.output)
    if args.command == 'prepare':
        report = prepare(args.truth, args.output)
    else:
        report = check(args.truth, args.prompts, args.responses, adjudications=load(args.adjudications) if args.adjudications else None)
        cx.write_json(args.output, report)
    print(cx.dumps({k: v for k, v in report.items() if k not in ('items', 'channels')}))
    return 0 if args.command == 'prepare' or report['overall']['passes'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
