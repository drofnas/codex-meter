#!/usr/bin/env python3
"""Verify recorded live comparisons and report 24-hour coverage without waivers."""
import argparse
import json
from pathlib import Path
from datetime import datetime
import statistics
import math

from common import save
from observe import FRAME, HEARTBEAT


def rows(path):
    if path.stat().st_size > 40 * 1024 * 1024:
        raise ValueError('evidence_file_exceeds_bound')
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def quota_matches(text, value):
    if text == '<1%': return 0 < value < 1
    if text == '>99%': return 99 < value < 100
    try: return abs(float(text.removesuffix('%')) - value) <= 1
    except ValueError: return False


def display_reset(publication):
    value=publication['reset_local']
    if publication.get('version',1)==1:return value[:16],value[17:]
    local=datetime.strptime(value,'%Y-%m-%d %H:%M %z')
    minutes=int(local.utcoffset().total_seconds())//60
    offset=('+' if minutes>0 else '-')+str(abs(minutes)//60) if minutes else '0'
    if abs(minutes)%60:offset+=':'+str(abs(minutes)%60).zfill(2)
    return local.strftime('%Y-%m-%d %-I:%M %p'),'('+offset+')'


def display_days(publication, as_of):
    labels, coverage, values = [], [], []
    for day in publication['days']:
        kind, value = day['coverage'], day['used_delta_pp']
        # The API sample may be later than the render of this observation.
        # Reconstruct future/unknown coverage at the render time in either direction.
        if kind in ('future', 'unknown'):
            kind = 'future' if day['start_at'] > as_of else 'unknown'
        labels.append(day['label'])
        coverage.append({'unknown': 'U', 'future': 'F', 'partial': 'P', 'complete': 'C'}[kind])
        if kind == 'unknown': text = '?'
        elif kind == 'future': text = '0>'
        else:
            text = '<1' if 0 < value < 1 else str(math.floor(value + .5))
            if kind == 'partial': text = '~' + text
        values.append(text)
    return ','.join(labels), ''.join(coverage), ','.join(values)


def verify_recovery(directory, recovery):
    events = json.loads((recovery / 'events.json').read_text())
    by_name = {r['event']: r for r in events}
    required = ('before_outage', 'api_removed', 'normal_schedule_restored', 'fresh_api_recovered')
    if any(name not in by_name for name in required):
        return {'result': 'pending_or_failed', 'errors': ['recovery_events_incomplete']}
    removed = by_name['api_removed']['epoch']
    restored = by_name['normal_schedule_restored']['epoch']
    before = by_name['before_outage']['observation']
    logs = [r for r in rows(directory / 'serial.jsonl') if r['epoch'] >= removed]
    failed = False
    retained, recovered = [], None
    errors = []
    for row in logs:
        if 'poll accepted=0 error=transport' in row['line']:
            failed = True
        frame = FRAME.search(row['line'])
        if not failed or not frame: continue
        if frame[1] == 'FRESH' and row['epoch'] >= restored and int(frame[10]) > before:
            recovered = row['epoch']; break
        retained.append(row)
        if frame[1] != 'STALE' or int(frame[10]) != before:
            errors.append('outage_did_not_retain_stale_observation')
    if not failed or not retained: errors.append('no_device_outage_evidence')
    if recovered is None or recovered - restored > 300: errors.append('no_timely_lcd_recovery')
    if restored - removed < 100: errors.append('outage_shorter_than_required')
    return {'result': 'passed' if not errors else 'pending_or_failed', 'errors': sorted(set(errors)),
            'outage_seconds': restored - removed, 'stale_retained_render_records': len(retained),
            'api_recovery_seconds': by_name['fresh_api_recovered']['epoch'] - restored,
            'lcd_recovery_seconds': recovered - restored if recovered else None}


def verify(directory, soak=False):
    status = json.loads((directory / 'status.json').read_text())
    logs = rows(directory / 'serial.jsonl')
    usage = rows(directory / 'usage.jsonl')
    sources = rows(directory / 'source.jsonl')
    host = rows(directory / 'host.jsonl')
    publications = {r['value']['observed_at']: r['value'] for r in usage if r['value'].get('observed_at') is not None}
    heartbeats, matched, unmatched, errors, fresh_lags = [], 0, 0, [], []
    measured_days = set()
    if status.get('status') != 'complete' or status.get('failures'):
        errors.append('observation_not_completed_successfully')
    for row in logs:
        line = row['line']
        if any(word in line.lower() for word in ('panic', 'guru meditation', 'watchdog')):
            errors.append('device_runtime_failure')
        if 'rst:' in line and (not status['intentional_initial_reset'] or row['elapsed'] > 30):
            errors.append('unexpected_boot')
        match = HEARTBEAT.search(line)
        if match: heartbeats.append({'epoch': row['epoch'], 'uptime': int(match[1]), 'heap': int(match[2])})
        frame = FRAME.search(line)
        if not frame or int(frame[10]) < 0: continue
        publication = publications.get(int(frame[10]))
        if publication is None:
            unmatched += 1; continue
        matched += 1
        if not quota_matches(frame[2], publication['remaining_percent']): errors.append('lcd_quota_mismatch')
        if (frame[3],frame[4]) != display_reset(publication):
            errors.append('lcd_reset_or_timezone_mismatch')
        expected_days = display_days(publication, int(row['epoch']))
        if (frame[7], frame[8], frame[9]) != expected_days:
            errors.append('lcd_daily_history_mismatch')
        elif frame[1] == 'FRESH' and any(c in expected_days[1] for c in 'PC'):
            measured_days.add(int(frame[10]))
        if frame[1] == 'FRESH':
            fresh_lags.append(row['epoch'] - int(frame[10]))
    if len(heartbeats) < 3 or matched < 3:
        errors.append('insufficient_live_device_evidence')
    if not measured_days:
        errors.append('no_measured_daily_history')
    if any(b['uptime'] <= a['uptime'] for a, b in zip(heartbeats, heartbeats[1:])):
        errors.append('uptime_reset')
    # Freshness acceptance is checked at newly displayed observations, not at
    # every repeated render of the same still-fresh publication (stale is 180s).
    seen = set(); first_display_lags = []
    for row in logs:
        frame = FRAME.search(row['line'])
        if frame and frame[1] == 'FRESH' and int(frame[10]) in publications and int(frame[10]) not in seen:
            seen.add(int(frame[10])); first_display_lags.append(row['epoch'] - int(frame[10]))
    if len(seen) < 3: errors.append('fewer_than_three_fresh_observations')
    if first_display_lags and min(first_display_lags) < 0: errors.append('fresh_observation_is_in_the_future')
    if first_display_lags and max(first_display_lags) > 120: errors.append('first_display_lag_over_120_seconds')
    comparisons = []
    for row in sources:
        source = row['value']
        if source.get('bucket') != 'codex': continue
        near = [u['value'] for u in usage if u['value'].get('observed_at') is not None and
                abs(u['value']['observed_at'] - source['observed_at']) <= 15]
        if near:
            nearest = min(near, key=lambda u: abs(u['observed_at'] - source['observed_at']))
            comparisons.append({'remaining_difference_pp': abs(source['remaining_percent'] - nearest['remaining_percent']),
                                'reset_difference_seconds': abs(source['resets_at'] - nearest['reset_at']),
                                'observation_difference_seconds': abs(source['observed_at'] - nearest['observed_at'])})
    if not comparisons: errors.append('no_near_simultaneous_source_comparison')
    if any(c['remaining_difference_pp'] > 1 or c['reset_difference_seconds'] for c in comparisons):
        errors.append('source_comparison_mismatch')
    heartbeat_span = heartbeats[-1]['epoch'] - heartbeats[0]['epoch'] if heartbeats else 0
    gaps = [b['epoch'] - a['epoch'] for a, b in zip(heartbeats, heartbeats[1:])]
    if soak:
        if status.get('status') != 'complete' or status.get('elapsed_seconds', 0) < 86400 or heartbeat_span < 86370:
            errors.append('24_hour_soak_incomplete')
        if not gaps or max(gaps) > 30: errors.append('serial_coverage_gap')
    quartile = max(1, len(heartbeats) // 4)
    host_quartile = max(1, len(host) // 4)
    return {'result': 'passed' if not errors else 'pending_or_failed', 'errors': sorted(set(errors)),
            'mode': '24_hour_soak' if soak else 'live_comparison', 'heartbeat_span_seconds': heartbeat_span,
            'maximum_heartbeat_gap_seconds': max(gaps, default=None), 'heartbeats': len(heartbeats),
            'matched_render_records': matched, 'unmatched_render_records': unmatched,
            'measured_daily_observations_displayed': len(measured_days),
            'unique_fresh_observations_displayed': len(seen), 'maximum_first_display_lag_seconds': max(first_display_lags, default=None),
            'near_simultaneous_source_comparisons': comparisons,
            'heap_first_quartile_median': statistics.median(r['heap'] for r in heartbeats[:quartile]) if heartbeats else None,
            'heap_last_quartile_median': statistics.median(r['heap'] for r in heartbeats[-quartile:]) if heartbeats else None,
            'host_combined_first_quartile_median_mib': statistics.median(r['combined_rss_bytes'] for r in host[:host_quartile]) / 1048576 if host else None,
            'host_combined_last_quartile_median_mib': statistics.median(r['combined_rss_bytes'] for r in host[-host_quartile:]) / 1048576 if host else None,
            'remaining_review': 'Inspect hourly memory/heap trends for bounded growth; actual LCD/settings confirmation and fault matrix remain separate gates.'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('--soak', action='store_true')
    p.add_argument('--recovery', type=Path, help='Verify the separately recorded real API outage against these serial logs')
    a = p.parse_args()
    result = verify_recovery(a.directory, a.recovery) if a.recovery else verify(a.directory, a.soak)
    save(a.directory / ('recovery-verification.json' if a.recovery else 'soak-verification.json' if a.soak else 'comparison.json'), result)
    print(json.dumps(result, indent=2))
    raise SystemExit(result['result'] != 'passed')
