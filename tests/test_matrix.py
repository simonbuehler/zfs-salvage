"""The README's capability matrix is generated from the pipeline test results and must not drift."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path

GEN = os.path.join(fx.REPO, 'dev', 'gen-matrix.py')
_spec = importlib.util.spec_from_file_location('gen_matrix', GEN)
gm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gm)


def load(name):
    with open(os.path.join(fx.TESTS, name)) as f:
        return json.load(f)


class Matrix(unittest.TestCase):
    def test_readme_matches_the_committed_results(self):
        r = subprocess.run([sys.executable, '-I', GEN, '--check'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_an_outdated_readme_is_caught(self):
        with tempfile.TemporaryDirectory() as d:
            readme = os.path.join(d, 'README.md')
            shutil.copy(os.path.join(fx.REPO, 'README.md'), readme)
            with open(readme) as f:
                text = f.read()
            with open(readme, 'w') as f:
                f.write(text.replace('| Compression | lz4, lzjb |', '| Compression | lz4, lzjb, zstd |'))
            r = subprocess.run([sys.executable, '-I', GEN, '--check'], capture_output=True, text=True,
                               env=dict(os.environ, GEN_MATRIX_README=readme))
            self.assertEqual(r.returncode, 1)
            self.assertIn('out of date', r.stderr)
            subprocess.run([sys.executable, '-I', GEN, '--write'], env=dict(os.environ, GEN_MATRIX_README=readme), capture_output=True)
            r = subprocess.run([sys.executable, '-I', GEN, '--check'], capture_output=True, text=True,
                               env=dict(os.environ, GEN_MATRIX_README=readme))
            self.assertEqual(r.returncode, 0, 'writing the table repairs it')

    def test_every_fixture_variant_stands_for_a_capability(self):
        mapped = set(load('capability_map.json')['variants'])
        known = set(fx.sidecars()) | set(fx.derived())
        self.assertEqual(sorted(known - mapped), [], 'a new fixture variant needs an entry in tests/capability_map.json')
        self.assertEqual(sorted(mapped - known), [], 'tests/capability_map.json names a variant that does not exist')

    def test_results_only_have_known_areas_and_clean_statuses(self):
        cmap, caps = load('capability_map.json'), load('capabilities.json')
        for e in caps['capabilities']:
            self.assertIn(e['area'], cmap['areas'])
            self.assertIn(e['status'], ('supported', 'not yet'), e)

    def test_a_mixed_result_is_not_rendered(self):
        caps = {'capabilities': [{'area': 'Compression', 'capability': 'lz4', 'status': 'mixed: FAIL/PASS', 'variants': ['x']}]}
        with self.assertRaises(SystemExit):
            gm.table(caps, load('capability_map.json'))

    def test_table_columns(self):
        caps = {'capabilities': [{'area': 'Compression', 'capability': 'lz4', 'status': 'supported', 'variants': ['a']},
                                 {'area': 'Compression', 'capability': 'zstd', 'status': 'not yet', 'variants': ['b']}]}
        lines = gm.table(caps, {'areas': ['Compression', 'Blocks'], 'by_hand_supported': [], 'untested_not_yet': [['Blocks', 'gang blocks']]}).splitlines()
        self.assertEqual(lines[2:], ['| Compression | lz4 | zstd |', '| Blocks | — | gang blocks |'])


if __name__ == '__main__':
    unittest.main()
