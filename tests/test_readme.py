"""The README promises things the tool must keep: the notice, a short quick start whose commands exist, and a
troubleshooting table that covers every message of doctor."""
import re
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path
import doctor  # noqa: E402
from test_launcher import run  # noqa: E402


def readme():
    with open(fx.REPO + '/README.md') as f:
        return f.read()


def section(text, title):
    m = re.search(rf'^#+ {re.escape(title)}\n(.*?)(?=^#{{1,3}} |\Z)', text, re.S | re.M)
    assert m, title
    return m.group(1)


class Readme(unittest.TestCase):
    def test_notice_at_the_top(self):
        top = '\n'.join(readme().splitlines()[:14]).lower()
        for word in ('only *reads*', 'no warranty', 'zpool import', 'make an image first'):
            self.assertIn(word, top)

    def test_quick_start_has_at_most_eight_numbered_steps(self):
        quick = readme().split('## Quick start')[1].split('Then follow the three steps')[0]
        steps = re.findall(r'^(\d+)\. \*\*', quick, re.M)
        self.assertEqual(steps, [str(i) for i in range(1, len(steps) + 1)])
        self.assertLessEqual(len(steps), 8)

    def test_every_launcher_command_in_the_readme_exists(self):
        help_text = run('help').stdout
        used = set(re.findall(r'\./zfs-salvage ([a-z][a-z-]+)', readme()))
        self.assertTrue({'doctor', 'make-case', 'scan', 'rank'} <= used, used)
        for sub in used:
            self.assertIn(sub, help_text, f'README uses ./zfs-salvage {sub}, the launcher does not know it')

    def test_troubleshooting_covers_every_check(self):
        table = section(readme(), 'Troubleshooting')
        rows = [[c.strip() for c in l.strip('|').split('|')] for l in table.splitlines() if l.startswith('| ') and not l.startswith('| Check')]
        self.assertEqual({r[0] for r in rows}, {name for name, _ in doctor.CHECKS})

    def test_troubleshooting_covers_every_message(self):
        """The beginning of each fixed message of doctor.py must appear in the table (lower case, up to the first colon or bracket)."""
        with open(fx.SCRIPTS + '/doctor.py') as f:
            source = f.read()
        heads = re.findall(r"return (?:FAIL|WARN|\(WARN if args\.no_root else FAIL\)), f?'([^'{]+)", source)
        self.assertGreater(len(heads), 12)
        table = section(readme(), 'Troubleshooting').lower()
        missing = [h for h in heads if re.split(r'[:(]', h)[0].strip().lower() not in table]
        self.assertEqual(missing, [], 'doctor says this, the README table does not')

    def test_troubleshooting_covers_every_refusal_of_make_case(self):
        """The fixed parts of what make-case says when it cannot help are in the README table."""
        import importlib.util
        spec = importlib.util.spec_from_file_location('make_case_refusals', fx.SCRIPTS + '/make-case.py')
        mc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mc)
        table = section(readme(), 'When `make-case` says NOT SUPPORTED').lower()
        self.assertGreaterEqual(len(mc.REFUSALS), 8)
        for key, (sentence, _hint) in mc.REFUSALS.items():
            for part in re.split(r'\{[^}]*\}', sentence):
                part = part.strip(' ,:')
                if len(part) >= 8:
                    self.assertIn(part.lower(), table, f'refusal {key!r}: "{part}" is not in the README table')


if __name__ == '__main__':
    unittest.main()
