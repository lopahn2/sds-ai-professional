"""Summarize selected original receipts from the matched dev comparison."""

from argparse import ArgumentParser
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import re

from evaluation.isolated_recovery_agent import EFFORT, MODELS
from evaluation.matched_recovery_campaign import APP, comparison_matrix, matched_start, write_new
from evaluation.single_baseline import estimate


TERMINAL = frozenset(('recovered', 'unrecovered'))
FAILURE_FIELDS = ('still_failed_before_cleanup', 'failed_before_cleanup', 'pressure_before_cleanup')
SHA256 = re.compile('[0-9a-f]{64}')


def usage_fields(arm, receipt):
    if arm != 'liveprobe':
        usage = receipt['standalone'].get('usage') or {}
        return {
            'tokens': usage.get('totalTokens'),
            'estimated_usd': estimate('single_' + arm, {
                'input_tokens': usage.get('inputTokens'), 'output_tokens': usage.get('outputTokens'),
                'cached_input_tokens': usage.get('cachedInputTokens'),
            }),
        }
    incidents = receipt.get('incidents') or [receipt.get('incident') or {}]
    calls = [call for row in incidents for call in row.get('calls') or [] if call.get('role') != 'interpreter']
    tokens = None
    if calls and all(type(call.get(key)) is int for call in calls for key in ('input_tokens', 'output_tokens')):
        tokens = sum(call['input_tokens'] + call['output_tokens'] for call in calls)
    costs = [call.get('estimated_usd') for call in calls]
    cost = round(sum(costs), 8) if costs and all(type(value) in (int, float) for value in costs) else None
    return {'tokens': tokens, 'estimated_usd': cost}


def metrics(receipt):
    case, arm, status = (receipt[key] for key in ('case', 'arm', 'status'))
    if not receipt.get('restored') or not receipt.get('fault') or receipt.get('cleanup_incomplete'):
        raise ValueError(f'missing fault or cleanup evidence for {case}/{arm}')
    if arm != 'liveprobe':
        agent = receipt.get('standalone') or {}
        if (agent.get('model'), agent.get('effort'), agent.get('ephemeral'), agent.get('status')) != (
            MODELS[arm], EFFORT, True, 'completed'
        ):
            raise ValueError(f'missing isolated model completion for {case}/{arm}')
    recovery_ms = receipt.get('recovery_ms')
    if status == 'recovered':
        if not receipt.get('verified_before_cleanup') or type(recovery_ms) is not int or recovery_ms <= 0:
            raise ValueError(f'missing independent recovery evidence for {case}/{arm}')
        if case == APP.id:
            regression = receipt.get('regression') or {}
            if (regression.get('reproduced'), regression.get('passed')) != (True, True):
                raise ValueError(f'missing before-and-after regression for {case}/{arm}')
    elif recovery_ms is not None or not any(receipt.get(key) for key in FAILURE_FIELDS):
        raise ValueError(f'missing independent evidence of continuing fault for {case}/{arm}')
    return {
        'first_error_ms': receipt.get('first_error_ms'), 'detection_ms': receipt.get('detected_ms'),
        'recovery_ms': recovery_ms, 'terminal_ms': receipt.get('trial_elapsed_ms'),
        **usage_fields(arm, receipt),
    }


def summarize(paths):
    matrix = {(row['case'], row['arm']): {**row, 'status': 'not_run'} for row in comparison_matrix()}
    selected = set()
    kinds = defaultdict(set)
    application_bases = set()
    for path in paths:
        raw = path.read_bytes()
        receipt = json.loads(raw)
        case, arm, status = (receipt[key] for key in ('case', 'arm', 'status'))
        key = (case, arm)
        if key not in matrix or key in selected or status not in TERMINAL | {'unobserved'}:
            raise ValueError(f'expected one original trial receipt for {case}/{arm}')
        selected.add(key)
        row = matrix[key]
        not_started = status == 'unobserved' and not (receipt.get('injected_at') or receipt.get('fault'))
        observed_status = 'not_run' if not_started else status
        row.update(status=observed_status, receipt=path.name, receipt_sha256=sha256(raw).hexdigest())
        if status not in TERMINAL:
            continue
        row.update(metrics(receipt))
        if case == APP.id:
            variant = receipt.get('variant_sha256') or ''
            allocation = receipt.get('allocation_sha256') or ''
            neutral = receipt.get('neutral_release_sha256') or ''
            deployment = receipt.get('deployment_uid') or ''
            if not SHA256.fullmatch(variant) or variant in kinds[case]:
                raise ValueError(f'missing or duplicate source variant for {case}/{arm}')
            if not SHA256.fullmatch(allocation) or not SHA256.fullmatch(neutral) or not deployment:
                raise ValueError(f'missing application allocation or deployment evidence for {case}/{arm}')
            application_bases.add((allocation, neutral, deployment))
            if len(application_bases) != 1:
                raise ValueError('application arms do not share the allocation, neutral release, and deployment')
            kinds[case].add(variant)
            row['variant_sha256'] = variant
        else:
            identity = matched_start(receipt['start_evidence'])
            if receipt.get('matched_start') != identity or kinds[case] - {identity}:
                raise ValueError(f'mismatched starting conditions for {case}/{arm}')
            kinds[case].add(identity)
            row['matched_start'] = identity

    counts = Counter(row['status'] for row in matrix.values())
    complete = [case for case in kinds if all(row['status'] in TERMINAL for row in matrix.values() if row['case'] == case)]
    return {
        'planned': len(matrix), 'observed': sum(counts[status] for status in TERMINAL),
        'complete_kubernetes_cases': [case for case in complete if case != APP.id],
        'application_variants_complete': APP.id in complete,
        'counts': dict(counts), 'rows': list(matrix.values()),
    }


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('receipts', nargs='+', type=Path)
    args = parser.parse_args()
    write_new(args.output, summarize(args.receipts))


if __name__ == '__main__':
    main()
