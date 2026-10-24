"""Damage inside the file tree that hides the newest states is reported, not passed over in silence; unreadable newer
trees of other datasets are not mistaken for it."""
import importlib.util
import json
import os
import stat
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path
from test_states import Rescue, read_text
from test_e2e_local import wait


def load_rank_trees():
    spec = importlib.util.spec_from_file_location('rank_trees_hidden', os.path.join(fx.SCRIPTS, 'rank-trees.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Counting(unittest.TestCase):
    def test_only_newer_unreadable_trees_of_about_the_same_size_count(self):
        rt = load_rank_trees()
        best = {'birth': 100, 'objects': 1000, 'ok': True}
        probed = {1: best, 2: {'birth': 120, 'objects': 1010, 'ok': False}, 3: {'birth': 130, 'objects': 980, 'ok': False},
                  4: {'birth': 140, 'objects': 40, 'ok': False},           # another, small dataset
                  5: {'birth': 90, 'objects': 1000, 'ok': False},          # older: overwritten long ago, normal
                  6: {'birth': 150, 'ok': False}}                          # size unknown
        self.assertEqual(rt.hidden_newer(probed, best), {'count': 2, 'newest_birth': 130, 'best_birth': 100})
        self.assertEqual(rt.hidden_newer(probed, None), {'count': 0})
        self.assertEqual(rt.hidden_newer({1: best}, best)['count'], 0)


@unittest.skipUnless(fx.have_images('modern-single') and fx.ensure_built(), 'needs gcc and the modern-single images')
class DamageInsideTheTree(unittest.TestCase):
    def test_newest_state_destroyed_inside_the_tree(self):
        r = Rescue('modern-single')
        try:
            rows = r.choose('docs')
            newest = rows[0]
            with open(os.path.join(r.state, 'status', 'probe_stats.json')) as f:
                self.assertFalse(json.load(f).get('hidden_newer', {}).get('count'))
            # damage inside the newest tree: its root stays intact, the child block written in the same TXG (every copy of
            # it) is destroyed – like a block that was overwritten later
            import zsread
            dev = r.case_dev
            c = newest['candidate']
            self.assertEqual(c['kind'], 'IND', 'the newest state is expected as a tree root')
            root = zsread.read_raw_candidate(zsread.Dev([dev]), int(c['offset'], 16), c['comp'], c['psize'], c['lsize'])
            child = max((zsread.BP(root[128 * i:128 * (i + 1)]) for i in range(len(root) // 128)), key=lambda bp: bp.birth)
            self.assertEqual(child.birth, newest['birth'])
            os.chmod(dev, 0o644)
            with open(dev, 'r+b') as f:
                for _, off, asize in child.dvas:
                    f.seek(zsread.LABEL_START + off)
                    f.write(os.urandom(asize))
            os.chmod(dev, 0o444)
            os.remove(os.path.join(r.state, 'status', 'probed.jsonl'))
            r.rank()
            with open(os.path.join(r.state, 'status', 'probe_stats.json')) as f:
                hidden = json.load(f)['hidden_newer']
            self.assertGreaterEqual(hidden['count'], 1)
            self.assertEqual(hidden['newest_birth'], newest['birth'])
            self.assertLess(r.candidates[0]['birth'], newest['birth'], 'the best readable state is now an older one')
            page = os.path.join(r.state, 'status', 'index.html')
            wait(lambda: 'damage inside the file tree hides them' in read_text(page), 'the warning on the status page', 60)
        finally:
            r.close()


if __name__ == '__main__':
    unittest.main()
