import unittest
import json
import tempfile
from pathlib import Path
from resources import parse_time, summarize
from observe import FRAME, HEARTBEAT
from common import ROOT, module
from verify import display_days, verify, verify_recovery
fault_verifier = module("fault_verifier", ROOT / "tools/firmware-check/verify_device.py")


class EvidenceChecks(unittest.TestCase):
    def test_daily_display_rounding_and_future_projection(self):
        p = {'days': [dict(label=str(i), coverage=c, used_delta_pp=v, start_at=100)
                      for i, (c, v) in enumerate([('unknown', None), ('future', 0),
                          ('partial', 0), ('partial', .1), ('complete', .5), ('complete', 2.5)])]}
        p['days'][0]['start_at'] = 90
        self.assertEqual(display_days(p, 99), ('0,1,2,3,4,5', 'UFPPCC', '?,0>,~0,~<1,<1,3'))
        self.assertEqual(display_days(p, 100), ('0,1,2,3,4,5', 'UUPPCC', '?,?,~0,~<1,<1,3'))
        self.assertEqual(p['days'][1]['coverage'], 'future')
        p['days'][1]['coverage'], p['days'][1]['used_delta_pp'] = 'unknown', None
        self.assertEqual(display_days(p, 99)[2], '?,0>,~0,~<1,<1,3')

    def test_live_verifier_requires_and_compares_daily_history(self):
        with tempfile.TemporaryDirectory() as name:
            out = Path(name)
            p = json.loads((ROOT/'contracts/v2/fixtures/normal.json').read_text())
            epoch = p['as_of']
            labels, coverage, values = display_days(p, epoch)
            (out/'status.json').write_text(json.dumps({'status': 'complete', 'intentional_initial_reset': False}))
            (out/'usage.jsonl').write_text(json.dumps({'value': p}) + '\n')
            for file in ('source.jsonl', 'host.jsonl'): (out/file).write_text('')
            def check(cov, vals):
                line = f'status=FRESH quota=42% reset=2026-09-16 6:30 PM offset=(-7) due=0 age=AGE 0S labels={labels} coverage={cov} values={vals} observation={p["observed_at"]} duration_ms=180 heap=162000 uptime=900'
                (out/'serial.jsonl').write_text(json.dumps({'epoch': epoch, 'line': line}) + '\n')
                return verify(out)['errors']
            self.assertNotIn('lcd_daily_history_mismatch', check(coverage, values))
            self.assertNotIn('no_measured_daily_history', check(coverage, values))
            self.assertIn('lcd_daily_history_mismatch', check('U'*len(p['days']), ','.join('?' for _ in p['days'])))
            for d in p['days']:
                d['coverage'], d['used_delta_pp'] = 'unknown', None
            (out/'usage.jsonl').write_text(json.dumps({'value': p}) + '\n')
            labels, coverage, values = display_days(p, epoch)
            errors = check(coverage, values)
            self.assertIn('no_measured_daily_history', errors)
            self.assertNotIn('lcd_daily_history_mismatch', errors)

    def test_resource_gate_rejects_incomplete_and_includes_child_cpu(self):
        rows = [{'elapsed': i * 5, 'collector_cpu_seconds': i / 100,
                 'api': {'cpu_ns': i * 1000000}, 'combined_rss_bytes': 30 * 1048576} for i in range(181)]
        report = summarize(rows, [{'cpu_seconds': 8}], 900)
        self.assertFalse(report['cpu_pass'])
        self.assertTrue(report['memory_pass'])
        with self.assertRaises(ValueError): summarize(rows[:-1], [{'cpu_seconds': 0}], 900)
        rows[90]['combined_rss_bytes'] = 64 * 1048576
        self.assertFalse(summarize(rows, [{'cpu_seconds': 0}], 900)['memory_pass'])
        rows[10]['elapsed'] += 2
        with self.assertRaises(ValueError): summarize(rows, [{'cpu_seconds': 0}], 900)

    def test_short_lived_command_accounting(self):
        row = parse_time('0.14 real 0.06 user 0.04 sys\n29081600 maximum resident set size\n')
        self.assertAlmostEqual(row['cpu_seconds'], .1)
        self.assertEqual(row['largest_individual_rss_bytes'], 29081600)
        with self.assertRaises(ValueError): parse_time('no timing')

    def test_same_timestamp_keeps_serial_stream_order(self):
        states = [{'log_index': 1, 'elapsed': 10, 'observation': 100},
                  {'log_index': 3, 'elapsed': 10, 'observation': 110}]
        result = {'log_index': 2, 'elapsed': 10}
        self.assertEqual(fault_verifier.post_result_states(states, result, 20), [states[1]])
        # A stale state after the result must still reach the retention assertion.
        states[1]['observation'] = 100
        self.assertEqual(fault_verifier.post_result_states(states, result, 20)[0]['observation'], 100)

    def test_device_records(self):
        line = 'status=FRESH quota=42% reset=2026-09-16 18:30 offset=-07:00 due=0 age=AGE 1M labels=W,Th,F,Sa,Su,M,T coverage=UUPFFFF values=?,?,~2,0>,0>,0>,0> observation=1789000000 duration_ms=180 heap=162000 uptime=900'
        m = FRAME.search(line)
        self.assertEqual(m[3], '2026-09-16 18:30')
        self.assertEqual(m[10], '1789000000')
        self.assertEqual(HEARTBEAT.search('alive uptime=900s free_heap=162000 bytes')[1], '900')

    def test_api_recovery_requires_actual_stale_retention_and_new_lcd_data(self):
        base = ROOT / 'artifacts/acceptance-tests'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as name:
            out = Path(name)
            events = [{'event': 'before_outage', 'epoch': 900, 'observation': 900},
                      {'event': 'api_removed', 'epoch': 1000},
                      {'event': 'normal_schedule_restored', 'epoch': 1100},
                      {'event': 'fresh_api_recovered', 'epoch': 1103}]
            (out / 'events.json').write_text(json.dumps(events))
            def frame(status, observation):
                return f'status={status} quota=97% reset=2026-09-18 21:43 offset=-07:00 due=0 age=AGE 2M labels=F,Sa,Su,M,T,W,Th coverage=PFFFFFF values=~0,0>,0>,0>,0>,0>,0> observation={observation} duration_ms=180 heap=163360 uptime=900'
            logs = [{'epoch': 1020, 'line': 'poll accepted=0 error=transport'},
                    {'epoch': 1021, 'line': frame('STALE', 900)}]
            def check():
                (out / 'serial.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in logs))
                return verify_recovery(out, out)
            self.assertNotEqual(check()['result'], 'passed')
            logs.append({'epoch': 1120, 'line': frame('FRESH', 1110)})
            self.assertEqual(check()['result'], 'passed')
            logs[1]['line'] = frame('FRESH', 900)
            self.assertIn('outage_did_not_retain_stale_observation', check()['errors'])


if __name__ == '__main__': unittest.main()
