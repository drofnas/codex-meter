#!/usr/bin/env python3
"""Verify captured CYD fault evidence, not just fixture-server success."""
import argparse
import json
from pathlib import Path
import re

FAILURES = {'malformed': 'payload', 'oversized': 'oversized', 'truncated': 'truncated',
            'version': 'payload', 'unauthorized': 'http_status', 'timeout': 'timeout'}


def post_result_states(states, result, next_request):
    # Multiple serial lines can share a capture timestamp. Stream order is
    # authoritative: a busy state printed before the result is not its result.
    return [s for s in states if s['log_index'] > result['log_index'] and s['elapsed'] < next_request]


def verify(path, steady=False):
    data = json.loads(path.read_text())
    assert not data['failures'], data['failures']
    events, logs = data['events'], data['logs']
    requests = [e for e in events if e['mode'] != 'outage']
    polls, states, heartbeats = [], [], []
    for log_index, entry in enumerate(logs):
        line = entry['line']
        assert not any(word in line.lower() for word in ['panic', 'guru meditation', 'watchdog']), line
        m = re.search(r'poll accepted=(\d) error=(\w+) duration_ms=(\d+) heap=(\d+) stack_free=(\d+)', line)
        if m:
            polls.append(dict(log_index=log_index, elapsed=entry['elapsed'], accepted=int(m[1]), error=m[2],
                              duration_ms=int(m[3]), heap=int(m[4]), stack_free=int(m[5])))
        m = re.search(r'state=(\w+) error=(\w+) observation=(-?\d+) age=(-?\d+) remaining=(-?[\d.]+).*heap=(\d+) uptime=(\d+)', line)
        if m:
            states.append(dict(log_index=log_index, elapsed=entry['elapsed'], status=m[1], error=m[2], observation=int(m[3]),
                               age=int(m[4]), remaining=float(m[5]), heap=int(m[6]), uptime=int(m[7])))
        m = re.search(r'alive uptime=(\d+)s free_heap=(\d+)', line)
        if m: heartbeats.append((int(m[1]), int(m[2])))
    assert requests and polls and states and len(heartbeats) >= 3, 'missing device evidence'
    boot_markers = sum('rst:' in e['line'] for e in logs)
    assert boot_markers <= 1, 'unexpected reboot during capture'
    assert all(a[0] < b[0] and b[0] - a[0] <= 12 for a, b in zip(heartbeats, heartbeats[1:])), 'heartbeat stalled'
    assert all(a['uptime'] < b['uptime'] for a, b in zip(states, states[1:])), 'uptime reset'
    for previous, current in zip(states, states[1:]):
        if previous['observation'] >= 0 and previous['observation'] == current['observation']:
            assert current['age'] >= previous['age'], 'age decreased without new observation'
            assert current['remaining'] == previous['remaining'], 'same observation changed quota'
    matched = []
    for i, event in enumerate(requests):
        next_request = requests[i + 1]['elapsed'] if i + 1 < len(requests) else float('inf')
        result = next((p for p in polls if event['elapsed'] <= p['elapsed'] < next_request), None)
        assert result is not None, ('request has no device result', event['mode'])
        expected = FAILURES.get(event['mode'], 'none')
        assert result['error'] == expected and result['accepted'] == (expected == 'none'), (event['mode'], result)
        assert result['duration_ms'] <= (10500 if steady else 2500), 'request deadline exceeded'
        matched.append(result)
        # When a post-result sample exists before the next request, it must retain
        # the source observation/value served or last accepted before this failure.
        after = post_result_states(states, result, next_request)
        if after:
            assert all(s['observation'] == event['observation'] and s['remaining'] == event['remaining'] for s in after), ('reading not retained', event['mode'])
    if steady:
        assert len(requests) >= 2 and all(p['accepted'] for p in matched), 'need multiple successful normal-cadence polls'
        gaps = [b['elapsed'] - a['elapsed'] for a, b in zip(requests, requests[1:])]
        assert all(59 <= g <= 62 for g in gaps), ('normal polling cadence', gaps)
    else:
        modes = {e['mode'] for e in events}
        assert set(FAILURES) | {'good', 'chunked', 'frozen', 'outage'} <= modes, 'fault sequence incomplete'
        frozen = [e for e in events if e['mode'] == 'frozen']
        assert len(frozen) >= 4, 'missing repeat-observation coverage'
        observed = frozen[0]['observation']
        same = [s for s in states if s['observation'] == observed]
        assert any(s['age'] >= 30 and s['status'] == 'stale' and s['error'] == 'none' for s in same), 'old HTTP success became fresh'
        outage = next(e for e in events if e['mode'] == 'outage')
        assert any(p['elapsed'] >= outage['elapsed'] and p['error'] == 'transport' for p in polls), 'missing stopped-service failure'
        assert any(p['elapsed'] > outage['elapsed'] + 16 and p['accepted'] for p in polls), 'missing API recovery'
        off = next((e['elapsed'] for e in logs if 'test_wifi_disable' in e['line']), None)
        on = next((e['elapsed'] for e in logs if 'test_wifi_enable' in e['line']), None)
        assert off is not None and on is not None and on - off >= 14, 'missing Wi-Fi interruption'
        assert any(off <= s['elapsed'] < on and s['error'] == 'wifi' and s['status'] == 'stale' for s in states), 'Wi-Fi loss not visible'
        assert any(p['elapsed'] > on and p['accepted'] for p in polls), 'missing Wi-Fi recovery'
    good_heap = [p['heap'] for p in matched if p['accepted']]
    summary = {'requests': len(requests), 'accepted': sum(p['accepted'] for p in polls),
               'observed_failure_codes': sorted({p['error'] for p in polls if not p['accepted']}),
               'maximum_request_ms': max(p['duration_ms'] for p in polls),
               'free_heap_min_bytes': min(s['heap'] for s in states),
               'free_heap_max_bytes': max(s['heap'] for s in states),
               'successful_poll_heap_first_bytes': good_heap[0], 'successful_poll_heap_last_bytes': good_heap[-1],
               'worker_stack_free_min_bytes': min(p['stack_free'] for p in polls),
               'heartbeat_samples': len(heartbeats), 'boot_markers': boot_markers,
               'maximum_uptime_seconds': max(s['uptime'] for s in states),
               'steady': steady, 'result': 'passed'}
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('events', type=Path)
    parser.add_argument('--steady', action='store_true')
    args = parser.parse_args()
    print(json.dumps(verify(args.events, args.steady), indent=2))
