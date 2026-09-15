#!/usr/bin/env python3
"""Run with the ESPHome interpreter; all configuration inputs are synthetic."""
import importlib.util
from pathlib import Path

import esphome.config_validation as cv

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('meter_config', ROOT / 'firmware/components/meter_network/__init__.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
base = {'url': 'http://192.0.2.1:8080/v2/usage', 'token': 'a' * 64,
        'poll_interval': cv.positive_time_period_milliseconds('60s'),
        'timeout': cv.positive_time_period_milliseconds('10s')}
assert module.validate_settings(dict(base)) == base
cases = [
    ('url', 'https://192.0.2.1:8080/v2/usage'), ('url', 'http://example.com:8080/v2/usage'),
    ('url', 'http://192.0.2.1/v2/usage'), ('url', 'http://192.0.2.1:80/v2/usage'),
    ('url', 'http://192.0.2.1:8080/v2/usage?x=1'), ('url', 'http://u:p@192.0.2.1:8080/v2/usage'),
    ('url', 'http://0.0.0.0:8080/v2/usage'), ('url', 'http://224.0.0.1:8080/v2/usage'),
    ('url', 'http://192.0.2.1:8080/healthz'), ('token', 'A'*64), ('token', 'a'*63),
    ('poll_interval', cv.positive_time_period_milliseconds('9s')),
    ('poll_interval', cv.positive_time_period_milliseconds('301s')),
    ('poll_interval', cv.positive_time_period_milliseconds('10.5s')),
    ('timeout', cv.positive_time_period_milliseconds('31s')),
    ('timeout', cv.positive_time_period_milliseconds('0.5s')),
]
for key, value in cases:
    config = dict(base); config[key] = value
    try: module.validate_settings(config)
    except cv.Invalid: pass
    else: raise AssertionError('invalid config accepted: ' + key)
config = dict(base, poll_interval=cv.positive_time_period_milliseconds('10s'),
              timeout=cv.positive_time_period_milliseconds('11s'))
try: module.validate_settings(config)
except cv.Invalid: pass
else: raise AssertionError('overlapping timeout accepted')
print(f'{len(cases)+2} configuration cases passed')
