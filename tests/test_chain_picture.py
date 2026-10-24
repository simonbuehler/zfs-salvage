"""The chain picture on the status page shows a break only where the diagnosis found one, and the detour only around it."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path

CODE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import status_page as sp
state = {'tree': ('neutral', '-'), 'dnodes': ('neutral', '-'), 'files': ('neutral', '-')}
nodes = sp.chain_nodes(state)
print(json.dumps({'states': [n[2] for n in nodes], 'subs': [n[1] for n in nodes], 'break': sp.chain_break(nodes),
                  'detour': 'Detour' in sp.chain_svg(state)}))
"""


def picture(case):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'case.json')
        with open(path, 'w') as f:
            json.dump(case, f)
        env = {k: v for k, v in os.environ.items() if not k.startswith('ZFS_SALVAGE_')}
        env.update(ZFS_SALVAGE_CASE=path, ZFS_SALVAGE_STATE_DIR=d, ZFS_SALVAGE_HOST='local')
        r = subprocess.run([sys.executable, '-I', '-c', CODE, fx.SCRIPTS], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr[-500:]
        return json.loads(r.stdout)


def case(**diag):
    return {'name': 'c', 'pool': {'name': 'p', 'devices': []}, 'target': {'dataset': 'p/home'},
            'diagnosis': dict({'uberblock_txg': 12, 'mos_txg': 12, 'dataset_listed': True}, **diag)}


class Chain(unittest.TestCase):
    def test_broken_objset_block(self):
        p = picture(case(situation='objset', objset_error='no valid copy', objset_birth=9))
        self.assertEqual(p['states'][:4], ['good', 'good', 'good', 'critical'])
        self.assertEqual(p['break'], 3)
        self.assertTrue(p['detour'])
        self.assertIn('no valid copy', p['subs'][3])

    def test_nothing_broken_draws_no_break_and_no_detour(self):
        p = picture(case(situation='intact', objset_birth=9))
        self.assertEqual(p['states'][:4], ['good', 'good', 'good', 'good'])
        self.assertIsNone(p['break'])
        self.assertFalse(p['detour'])

    def test_destroyed_dataset_breaks_at_the_dataset(self):
        p = picture(case(situation='missing', dataset_listed=False))
        self.assertEqual(p['states'][:4], ['good', 'good', 'critical', 'neutral'])
        self.assertEqual(p['break'], 2)
        self.assertIn('destroyed', p['subs'][2])

    def test_unreadable_metadata_breaks_at_the_mos(self):
        p = picture(case(situation='mos', mos_txg=None, dataset_listed=False))
        self.assertEqual(p['states'][:4], ['good', 'critical', 'neutral', 'neutral'])
        self.assertEqual(p['break'], 1)
        self.assertTrue(p['detour'])

    def test_a_case_without_any_diagnosis_claims_nothing(self):
        p = picture({'name': 'c', 'pool': {'name': 'p', 'devices': []}, 'target': {'dataset': 'p/home'}})
        self.assertNotIn('critical', p['states'])
        self.assertIsNone(p['break'])

    def test_a_hand_written_case_with_a_break_entry_still_works(self):
        p = picture({'name': 'c', 'pool': {'name': 'p', 'devices': []}, 'crash_txg': 100, 'open_txg': 90,
                     'target': {'dataset': 'p/home', 'break': {'birth_txg': 50, 'reason': 'ECKSUM'}}})
        self.assertEqual(p['break'], 3)
        self.assertIn('ECKSUM', p['subs'][3])
        self.assertIn('last state before the crash', p['subs'][0])


if __name__ == '__main__':
    unittest.main()
