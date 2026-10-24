#!/usr/bin/env python3
"""Render the capability matrix of the README from the pipeline test results.

tests/run.sh --matrix writes tests/capabilities.json (what each fixture variant did), tests/capability_map.json says
which capability each variant stands for. This script turns both into the table between the markers
`<!-- capability-matrix:start -->` and `<!-- capability-matrix:end -->` of README.md.

Usage: scripts/gen-matrix.py           print the table
       scripts/gen-matrix.py --write   update README.md
       scripts/gen-matrix.py --check   exit 1 if README.md is out of date (CI)
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START, END = '<!-- capability-matrix:start -->', '<!-- capability-matrix:end -->'


def load(name):
    with open(os.path.join(ROOT, 'tests', name)) as f:
        return json.load(f)


def table(caps, cmap):
    """Markdown table: per area what passed in the pipeline test and what is a known failure or untested."""
    cells = {a: ([], []) for a in cmap['areas']}
    for e in caps['capabilities']:
        if e['status'] == 'supported':
            cells[e['area']][0].append(e['capability'])
        elif e['status'] == 'not yet':
            cells[e['area']][1].append(e['capability'])
        else:
            raise SystemExit(f'{e["area"]}: {e["capability"]} has the status "{e["status"]}": run tests/run.sh --matrix again')
    for area, cap in cmap['by_hand_supported']:
        cells[area][0].append(cap)
    for area, cap in cmap['untested_not_yet']:
        cells[area][1].append(cap)
    lines = ['| Area | Supported | Not yet |', '|---|---|---|']
    for area in cmap['areas']:
        ok, no = cells[area]
        lines.append(f'| {area} | {", ".join(ok) or "—"} | {", ".join(no) or "—"} |')
    return '\n'.join(lines)


def rendered():
    return table(load('capabilities.json'), load('capability_map.json'))


def readme_path():
    return os.environ.get('GEN_MATRIX_README') or os.path.join(ROOT, 'README.md')


def current(text):
    a, b = text.index(START) + len(START), text.index(END)
    return text[a:b].strip('\n')


def main(argv):
    want = rendered()
    with open(readme_path()) as f:
        text = f.read()
    if '--check' in argv:
        if current(text) != want:
            print('README.md: the capability matrix is out of date; run scripts/gen-matrix.py --write', file=sys.stderr)
            return 1
        print('README.md: capability matrix is up to date')
        return 0
    if '--write' in argv:
        a, b = text.index(START) + len(START), text.index(END)
        with open(readme_path(), 'w') as f:
            f.write(text[:a] + '\n' + want + '\n' + text[b:])
        print('README.md updated')
        return 0
    print(want)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
