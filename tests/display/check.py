#!/usr/bin/env python3
"""Oracle-checked view semantics, sanitized pixel bounds, and repeatable images.

Run with the contract-check Python environment (jsonschema required).
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('oracle', ROOT / 'tests/contracts/validate.py')
oracle = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(oracle)
sys.path.insert(0,str(ROOT/'tests/contracts'))
from calendar_fixture import calendarize, scenario


def fixtures():
    source = ROOT / 'contracts/v2/fixtures'
    result = {Path(i['file']).stem: json.loads((source/i['file']).read_text())
              for i in json.loads((source/'manifest.json').read_text())
              if i.get('kind', 'usage') == 'usage' and i['expect'] == 'valid'}
    normal = result['normal']
    result['full'] = dict(copy.deepcopy(normal), remaining_percent=100)
    result['almost-full'] = dict(copy.deepcopy(normal), remaining_percent=99.6)
    m = copy.deepcopy(normal); m['remaining_percent'] = .4
    m['days'][0]['used_delta_pp'] = 0  # measured zero, partial zero, future zero together
    m['days'][1]['used_delta_pp'] = .2
    result['zeros-and-fraction'] = m
    m = copy.deepcopy(normal); m['days'][0]['used_delta_pp'] = 100
    m['days'][1]['used_delta_pp'] = 0
    result['full-bar'] = m
    m = copy.deepcopy(normal); m['days'][0]['used_delta_pp'] = 50
    m['days'][1]['used_delta_pp'] = 20
    result['portrait-half-bar'] = m
    m = copy.deepcopy(normal); m['days'][0]['used_delta_pp'] = .5
    m['days'][1]['used_delta_pp'] = 99.5
    result['portrait-rounding'] = m
    m = scenario(normal, '2026-09-19T01:11')
    m['observed_at'] = m['updated_at'] = m['as_of'] = int(oracle.datetime(2026,9,15,12,tzinfo=ZoneInfo(m['timezone'])).timestamp())
    m['remaining_percent'] = 64; m['resets_available'] = 2
    m['resets_expire_at'] = m['as_of'] + 8*86400
    for i,d in enumerate(m['days']):
        d['used_delta_pp'] = [6,0,20,10,0,0,0,0][i]
        d['coverage'] = 'complete' if d['end_at']<=m['as_of'] else 'partial' if d['start_at']<=m['as_of'] else 'future'
    result['portrait-example'] = m
    for name, zone in [('offset-positive', 'Pacific/Kiritimati'), ('offset-negative', 'Etc/GMT+12')]:
        m = copy.deepcopy(normal); m['timezone'] = zone
        m['reset_local'] = oracle.reset_text(m['reset_at'], ZoneInfo(zone))
        for d in m['days']:
            d['label'] = oracle.LABELS[oracle.datetime.fromtimestamp(d['start_at'], ZoneInfo(zone)).weekday()]
        result[name] = calendarize(m)
    m = copy.deepcopy(normal); shift = 4070995200 - m['observed_at']
    for k in ['observed_at', 'updated_at', 'as_of', 'reset_at']: m[k] += shift
    for k in ['start_at', 'end_at']: m['cycle'][k] += shift
    m['reset_local'] = oracle.reset_text(m['reset_at'], ZoneInfo(m['timezone']))
    for d in m['days']:
        d['start_at'] += shift; d['end_at'] += shift
        d['label'] = oracle.LABELS[oracle.datetime.fromtimestamp(d['start_at'], ZoneInfo(m['timezone'])).weekday()]
    result['long-date'] = calendarize(m)
    m = copy.deepcopy(normal)
    m['observed_at'] = m['updated_at'] = m['as_of'] = m['reset_at'] - 5
    for d in m['days']:
        if d['coverage'] == 'future': d.update(coverage='unknown', used_delta_pp=None)
    result['expires-offline'] = m
    return result


def portrait_pixels(image, frame):
    magic, dimensions, maximum, pixels = image.read_bytes().split(b'\n',3)
    assert (magic, dimensions, maximum)==(b'P6',b'240 320',b'255')
    assert len(pixels)==240*320*3
    def rgb(color):
        return bytes(((color>>16)&0xE0,(color>>8)&0xE0,color&0xC0))
    bg, track, ink = rgb(0x101820), rgb(0x324450), rgb(0xF3F6F7)
    def pixel(x,y):
        return pixels[(y*240+x)*3:(y*240+x)*3+3]
    count=len(frame['bars'])
    for i,b in enumerate(frame['bars']):
        y=162+i*(18 if count==9 else 20)
        fill=rgb(0xFFE060 if i==frame['today'] else 0x63DFC1)
        # Sample above and below centered text: fills must be solid and on a fixed scale.
        for row in (0,2,11,13):
            assert all(pixel(60+x,y+row)==(fill if x<b['width'] else track) for x in range(168)), (image.name,i,'fill')
        label_colors={pixel(x,row) for row in range(y,y+14) for x in range(12,46)}
        assert label_colors=={bg,fill}, (image.name,i,'weekday color')
        # A glyph over bright fill uses dark ink; one over the track stays light.
        value_width=6*len(b['portrait_value'])-1
        left=60+(168-value_width)//2
        glyph_pixels=0
        for x in range(left,left+value_width):
            on_fill=x<60+b['width']
            background=fill if on_fill else track
            for row in range(y+4,y+11):
                actual=pixel(x,row)
                if actual!=background:
                    assert actual==(bg if on_fill else ink), (image.name,i,'value contrast')
                    glyph_pixels+=1
        assert glyph_pixels>0
    bottom=162+(count-1)*(18 if count==9 else 20)+14
    assert all(pixel(x,y)==bg for y in range(bottom,320) for x in range(240)), 'unexpected footer'
    assert all(pixel(x,y)==bg for y in range(127,135) for x in range(240)), 'unexpected age text'
    if not frame['resets']:
        assert all(pixel(x,y)==bg for y in range(32) for x in range(88,240)), 'unexpected header status'


def run(output):
    OUT = output.resolve()
    if not OUT.is_relative_to(ROOT / 'artifacts'):
        raise ValueError('Use an output directory under repository artifacts')
    OUT.mkdir(parents=True, exist_ok=True)
    binary = OUT/'render'
    subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-g',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-I', str(ROOT),
                    str(Path(__file__).with_name('native.cpp')), '-o', str(binary)], check=True)
    orientation_binary = OUT/'orientation-check'
    subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-g',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-I', str(ROOT),
                    str(Path(__file__).with_name('orientation.cpp')), '-o', str(orientation_binary)], check=True)
    subprocess.run([str(orientation_binary)], check=True, timeout=15)
    cases = fixtures(); manifest = []; timings = []; frame_bytes = set()
    landscape_hashes=json.loads(Path(__file__).with_name('landscape.sha256.json').read_text())
    landscape_checked=set()
    for name, value in cases.items():
        raw = json.dumps(value, separators=(',', ':')).encode()
        oracle.validate(raw, 'usage', version=2)
        fixture = OUT/(name+'.json'); fixture.write_bytes(raw)
        scenarios = [(name, 0, 'online')]
        if name == 'normal': scenarios += [('offline', 181000, 'offline'), ('expired-outage', 604800000, 'offline'), ('very-old', 2**50, 'offline')]
        if name == 'resets-one-second': scenarios += [('resets-expired', 1000, 'online')]
        if name == 'resets-four-days': scenarios += [('resets-cross-red', 1000, 'online')]
        if name == 'resets-blue': scenarios += [('resets-cross-yellow', 86400000, 'offline')]
        if name == 'expires-offline': scenarios += [('expires-after-5s', 6000, 'offline')]
        if name in ('normal','saturday-eight','spring-nine','fall-eight','unknown-history'):
            starts=[d['start_at'] for d in value['days'] if d['start_at']>value['as_of']]
            for boundary in sorted({starts[0],starts[-1]}):
                for shift in (-1,0,1000):
                    elapsed=(boundary-value['as_of'])*1000+shift
                    scenarios.append((f'{name}-boundary-{boundary}-{shift}',elapsed,'offline'))
        for label, elapsed, network in scenarios:
            for position, suffix in enumerate(('', '-portrait', '-inverted', '-portrait-inverted')):
                name_with_position = label + suffix
                image = OUT/(name_with_position+'.ppm')
                p = subprocess.run([str(binary), str(fixture), str(image), str(elapsed), network, str(position)],
                                   capture_output=True, text=True, check=True, timeout=15)
                f = json.loads(p.stdout)
                if name_with_position in landscape_hashes:
                    assert hashlib.sha256(image.read_bytes()).hexdigest()==landscape_hashes[name_with_position], ('landscape changed',name_with_position)
                    landscape_checked.add(name_with_position)
                timing = re.search(r'frame_bytes=(\d+) sanitized_render_mean_us=([\d.]+)', p.stderr)
                assert timing, p.stderr
                frame_bytes.add(int(timing[1])); timings.append(float(timing[2]))
                count, expiry = value['resets_available'], value.get('resets_expire_at')
                instant = value['as_of'] + elapsed//1000
                visible = count is not None and count > 0 and (expiry is None or instant < expiry)
                assert f['resets'] == (str(count) if visible else '')
                color = 0xAAB8C2
                if visible and expiry is not None and value['reason'] != 'clock_error':
                    days = (expiry-instant)/86400
                    color = 0xFF6060 if days < 4 else 0xFFE060 if days <= 7 else 0x60BFFF
                assert f['resets_color'] == color
                if value['observed_at'] is None:
                    assert f['status'] == 'WAIT' and f['quota'] == '--%' and f['gauge'] == 0
                    assert f['reset'] == f['offset'] == '--'
                    assert all(b['value'] == '?' and b['label'] == '--' for b in f['bars'])
                    assert f['today']==-1
                    assert all(b['portrait_value']=='?' and b['portrait_label']=='--' and b['width']==0 for b in f['bars'])
                else:
                    local=oracle.datetime.fromtimestamp(value['reset_at'],ZoneInfo(value['timezone']))
                    assert f['reset'] == local.strftime('%Y-%m-%d %-I:%M %p')
                    offset=int(local.utcoffset().total_seconds())//60
                    compact=('+' if offset>0 else '-')+str(abs(offset)//60) if offset else '0'
                    if abs(offset)%60: compact+=':'+str(abs(offset)%60).zfill(2)
                    assert f['offset']=='('+compact+')'
                    assert [b['label'] for b in f['bars']] == [d['label'] for d in value['days']]
                    today=next((i for i,d in enumerate(value['days']) if d['start_at']<=instant<d['end_at']),-1)
                    assert f['today']==today, (name_with_position, 'current interval')
                    assert f['quota'] != '--%' and 0 <= f['gauge'] <= 296
                    if network == 'offline' or value['status'] == 'stale': assert f['status'] == 'STALE'
                    if value['as_of'] + elapsed//1000 >= value['reset_at']: assert f['due']
                    for b, d in zip(f['bars'], value['days']):
                        labels=dict(M='MON',T='TUE',W='WED',Th='THU',F='FRI',Sa='SAT',Su='SUN')
                        assert b['portrait_label']==labels[d['label']]
                        expired_future = d['coverage'] == 'future' and d['start_at'] <= value['as_of'] + elapsed//1000
                        if d['coverage'] == 'unknown' or expired_future: assert b['value'] == '?' and b['height'] == 0
                        elif d['coverage'] == 'future': assert b['value'] == '0>' and b['height'] == 0
                        else:
                            assert b['value'].startswith('~') == (d['coverage'] == 'partial')
                            assert (b['height'] == 0) == (d['used_delta_pp'] == 0)
                            assert 0 <= b['height'] <= 38
                        if d['coverage']=='unknown' or expired_future:
                            assert b['portrait_value']=='?' and b['width']==0
                        else:
                            delta=d['used_delta_pp']
                            assert b['portrait_value']==f'{math.floor(delta+.5)}%'
                            assert b['width']==(max(1,math.floor(delta*168/100+.5)) if delta>0 else 0)
                    if label.startswith('expired') or label == 'expires-after-5s':
                        assert f['quota'] == '55%' and f['bars'][0]['value'] == '30', 'reset fabricated new quota/history'
                if name == 'full': assert f['quota'] == '100%' and f['gauge'] == 296
                if name == 'almost-full': assert f['quota'] == '>99%'
                if name == 'zeros-and-fraction': assert f['quota'] == '<1%'
                if name == 'zero-remaining': assert f['quota'] == '0%' and f['gauge'] == 0
                if position%2: portrait_pixels(image,f)
                manifest.append(dict(name=name_with_position, scenario=label, position=position,
                                     width=240 if position % 2 else 320, height=320 if position % 2 else 240,
                                     frame=f, ppm=str(image.relative_to(ROOT))))
    assert len(frame_bytes)==1 and max(frame_bytes)<=256
    assert landscape_checked==landscape_hashes.keys(), 'missing landscape regression scenarios'
    summary = dict(oracle_fixtures=len(cases), rendered_cases=len(manifest), sanitized_bounds='passed',
                   frame_bytes=max(frame_bytes), iterations_per_case=100, orientations=4,
                   orientation_input_persistence='passed',
                   portrait_pixels='passed', landscape_unchanged=len(landscape_checked),
                   sanitized_render_mean_us_min=min(timings), sanitized_render_mean_us_max=max(timings))
    (OUT/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    (OUT/'measurement.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, help='Keep fixture images and results under artifacts/')
    args = p.parse_args()
    if args.output:
        run(args.output)
    else:
        (ROOT / 'artifacts').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='display-', dir=ROOT / 'artifacts') as directory:
            run(Path(directory))
