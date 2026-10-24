"""Choosing the file system from the groups of verified trees, without knowing its root names."""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path
from test_e2e_local import api, free_port, wait
from test_visible_errors import env_for


def load_rank_trees():
    spec = importlib.util.spec_from_file_location('rank_trees_groups', os.path.join(fx.SCRIPTS, 'rank-trees.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_json(path):
    with open(path) as f:
        return json.load(f)


def tree(birth, objects, *names):
    return {'ok': True, 'birth': birth, 'objects': objects, 'root': list(names)}


class Grouping(unittest.TestCase):
    def test_small_changes_stay_in_one_group_and_names_are_distinct(self):
        rt = load_rank_trees()
        probed = {1: tree(50, 70, 'docs', 'photos', 'a', 'bin', 'lost+found'), 2: tree(40, 60, 'docs', 'photos', 'a', 'lost+found'),
                  3: tree(45, 40, 'log', 'cache', 'lost+found'), 4: {'ok': False, 'birth': 99, 'root': ['docs']},
                  5: {'ok': True, 'birth': 98, 'type': 'zvol', 'volsize': 1 << 20}, 6: tree(97, 0)}
        groups = rt.tree_groups(probed, ['log'])
        self.assertEqual(len(groups), 2, 'unreadable, zvol and empty trees are not groups')
        home, var = groups
        self.assertEqual((home['trees'], home['newest_txg'], home['oldest_txg'], home['objects']), (2, 50, 40, 70))
        self.assertEqual(home['names'], ['a', 'bin', 'docs', 'photos'], 'names both have (lost+found) identify nothing')
        self.assertEqual(var['names'], ['cache', 'log'])
        self.assertEqual([g['chosen'] for g in groups], [False, True])

    def test_no_trees_no_groups(self):
        self.assertEqual(load_rank_trees().tree_groups({}), [])


@unittest.skipUnless(fx.have_images('modern-twodatasets') and fx.ensure_built(), 'needs gcc and the modern-twodatasets images')
class ChooseInTheUi(unittest.TestCase):
    def test_two_file_systems_offered_and_chosen(self):
        variant = 'modern-twodatasets'
        side = fx.sidecars()[variant]
        home_names = sorted({f['path'].split('/')[1] for f in side['files']})
        work = fx.tmpdir()
        proc = None
        try:
            dev = fx.unpack(variant, os.path.join(work, 'img'))[0]
            os.chmod(dev, os.stat(dev).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            raw = os.path.join(work, 'raw')
            case = {'name': 'groups', 'pool': {'name': side['pool'], 'devices': [{'path': dev, 'vdev': 0}]},
                    'profile': {'label_start': side['label_start'], 'compression': [], 'checksum': []},
                    'timeline': [{'txg': side['newest_txg'], 'time': 1}], 'compatibility': [],
                    'target': {'dataset': side['target'], 'root_names': []},          # nothing known about the target
                    'scan': {'device': 'd0', 'chunk_dir': raw, 'log': os.path.join(work, 'scan.log')}}
            cpath = os.path.join(work, 'case.json')
            with open(cpath, 'w') as f:
                json.dump(case, f)
            state = os.path.join(work, 'state')
            env = env_for(state, 'down', ZFS_SALVAGE_CASE=cpath, ZFS_SALVAGE_CATALOG_DIR=os.path.join(work, 'catalog'))
            del env['ZFS_SALVAGE_HOST']
            env.pop('ZFS_SALVAGE_READER', None)
            r = subprocess.run(['bash', os.path.join(fx.SCRIPTS, 'scan-device.sh'), dev, '64', raw], env=env,
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stdout[-500:])
            rank = [sys.executable, '-I', os.path.join(fx.SCRIPTS, 'rank-trees.py')]
            r = subprocess.run(rank, env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-500:])
            cands = os.path.join(state, 'status', 'target_candidates.json')
            self.assertEqual(read_json(cands), [], 'without root names nothing is the target yet')

            port = free_port()
            proc = subprocess.Popen([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'web.py')],
                                    env=dict(env, SALVAGE_UI_PORT=str(port)), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            wait(lambda: api(port, '/api/workflow'), 'the web server')
            g = api(port, '/api/target/groups')
            self.assertTrue(g['needs_choice'])
            # the pool's own root dataset is a file system too: its root directory holds the mount points
            self.assertEqual(sorted(tuple(x['names']) for x in g['groups']),
                             sorted([tuple(home_names), ('cache', 'log'), ('home', 'var')]), g)
            homeg = next(x for x in g['groups'] if 'docs' in x['names'])
            varg = next(x for x in g['groups'] if 'log' in x['names'])
            page = os.path.join(state, 'status', 'index.html')
            wait(lambda: os.path.exists(page), 'the status page', 60)
            with open(page) as f:
                self.assertIn('Found 3 different file systems – which one do you want to recover?', f.read())

            for chosen, other in ((homeg, varg), (varg, homeg)):
                self.assertTrue(api(port, '/api/target/choose', {'names': chosen['names']})['ok'])
                self.assertEqual(read_json(cpath)['target']['root_names'], chosen['names'])
                table = wait(lambda: (lambda t: t if t and set(t[0]['root'].split(', ')) & set(chosen['names']) else None)(
                    read_json(cands)), 'the candidates of the chosen file system', 60)
                for row in table:
                    shown = set(row['root'].replace(' …', '').split(', '))
                    self.assertFalse(shown & set(other['names']), f'{row["root"]} belongs to the other group')
                self.assertFalse(api(port, '/api/target/groups')['needs_choice'])

            for bad in ({}, {'names': []}, {'names': [1]}):
                with self.assertRaises(Exception) as cm:
                    api(port, '/api/target/choose', bad)
                self.assertEqual(getattr(cm.exception, 'code', None), 400)
        finally:
            if proc:
                proc.terminate()
                proc.wait(timeout=10)
            fx.rmtree(work)


if __name__ == '__main__':
    unittest.main()
