#!/usr/bin/env python3
"""Check the production touch settings against captured CYD corner readings.

Use the ESPHome Python (PyYAML required). No private configuration is emitted.
"""
import argparse
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]


def run(output):
    output = output.resolve()
    if not output.is_relative_to(ROOT/'artifacts'):
        raise ValueError('Use an output directory under repository artifacts')
    output.mkdir(parents=True, exist_ok=True)
    # BaseLoader handles !secret/anchors as inert strings/mappings. Only public
    # calibration numbers and booleans reach the native test or its output.
    config = yaml.load((ROOT/'codex-meter-1.yaml').read_text(), Loader=yaml.BaseLoader)
    settings = config['meter_network']
    values = [str(int(settings['touch_calibration'][key])) for key in ('x_min','x_max','y_min','y_max')]
    for key in ('swap_xy','mirror_x','mirror_y'):
        value = settings['touch_transform'][key].lower()
        if value not in ('true', 'false'):
            raise ValueError('Use explicit true/false touch transform values')
        values.append('1' if value == 'true' else '0')
    if config['touchscreen'][0].get('transform'):
        raise ValueError('Raw touch input must not have a second framework transform')
    binary = output/'orientation-check'
    subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror','-g',
                    '-fsanitize=address,undefined','-fno-omit-frame-pointer','-I',str(ROOT),
                    str(Path(__file__).with_name('orientation.cpp')),'-o',str(binary)],check=True)
    subprocess.run([str(binary),*values],check=True,timeout=15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/title-rotation/touch-check')
    run(parser.parse_args().output)
