#!/usr/bin/env python3
"""Convert native PPMs to PNGs and a fixture sheet; requires Pillow."""
import argparse
import json
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'artifacts/cm-007/native'
SELECT = ['normal', 'zero-remaining', 'full', 'unknown-history', 'zeros-and-fraction',
          'offline', 'unavailable', 'dst-spring', 'dst-fall', 'offset-positive', 'long-date', 'expires-after-5s']

def contact_sheet(output, names, filename, columns=3):
    images = [Image.open(output/(name+'.png')) for name in names]
    cell_width = max(image.width for image in images) + 16
    cell_height = max(image.height for image in images) + 30
    sheet = Image.new('RGB', (columns*cell_width, ((len(names)+columns-1)//columns)*cell_height), '#20262e')
    draw = ImageDraw.Draw(sheet)
    for i, (name, image) in enumerate(zip(names, images)):
        x = (i % columns)*cell_width + 8
        y = (i // columns)*cell_height + 8
        draw.text((x, y), name, fill='white')
        sheet.paste(image, (x, y+20))
    sheet.save(output/filename)


def run(output):
    output = output.resolve()
    if not output.is_relative_to(ROOT/'artifacts'):
        raise ValueError('Use an output directory under repository artifacts')
    manifest = json.loads((output/'manifest.json').read_text())
    names = {item['name'] for item in manifest}
    for item in manifest:
        with Image.open(ROOT/item['ppm']) as source:
            source.save(output/(item['name']+'.png'))
    contact_sheet(output, SELECT, 'fixture-sheet.png')
    if 'normal-portrait' in names:
        contact_sheet(output, [name+'-portrait' for name in SELECT], 'portrait-fixture-sheet.png')
        contact_sheet(output, ['normal', 'normal-portrait', 'normal-inverted', 'normal-portrait-inverted'],
                      'orientation-sheet.png', columns=4)
    if 'resets-blue' in names:
        reset_names = ['resets-blue', 'resets-seven-days', 'resets-red', 'resets-zero', 'resets-null-expiry', 'resets-max-count']
        contact_sheet(output, reset_names, 'reset-sheet.png')
        contact_sheet(output, [name+'-portrait' for name in reset_names], 'reset-portrait-sheet.png')
    print(f'Created {len(manifest)} PNGs and fixture sheets under {output.relative_to(ROOT)}.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUT)
    run(parser.parse_args().output)
