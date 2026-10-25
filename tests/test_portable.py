"""tools/check-portable.sh: the glibc limit that decides whether the binaries run on old systems.
objdump and file are replaced by shims, so the test does not depend on the compiler of this machine; building in
the manylinux2014 container (tools/build-portable.sh) needs Docker and network and is run by the CI."""
import os
import subprocess
import tempfile
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path

CHECK = os.path.join(fx.REPO, 'tools', 'check-portable.sh')


def check(objdump_out='', file_out='ELF 64-bit LSB shared object, dynamically linked'):
    with tempfile.TemporaryDirectory() as d:
        for name, text in (('objdump', objdump_out), ('file', file_out)):
            shim = os.path.join(d, name)
            with open(shim, 'w') as f:
                f.write(f"#!/bin/bash\ncat <<'EOT'\n{text}\nEOT\n")
            os.chmod(shim, 0o755)
        binary = os.path.join(d, 'libx.so')
        with open(binary, 'w') as f:
            f.write('x')
        r = subprocess.run(['bash', CHECK, binary], capture_output=True, text=True, env=dict(os.environ, PATH=f'{d}:{os.environ["PATH"]}'))
        return r.returncode, r.stdout


def symbols(*versions):
    return '\n'.join(f'0000 g    DF .text  0000  GLIBC_{v}  fn{i}' for i, v in enumerate(versions))


class GlibcLimit(unittest.TestCase):
    def test_old_enough(self):
        rc, out = check(symbols('2.2.5', '2.14'))
        self.assertEqual(rc, 0, out)
        self.assertIn('glibc 2.14', out)

    def test_limit_itself_is_allowed(self):
        self.assertEqual(check(symbols('2.17'))[0], 0)

    def test_one_newer_symbol_is_enough_to_fail(self):
        rc, out = check(symbols('2.2.5', '2.18'))
        self.assertEqual(rc, 1)
        self.assertIn('needs glibc 2.18', out)

    def test_versions_compare_as_numbers_not_text(self):
        # as text "2.4" > "2.17"; as versions it is older
        self.assertEqual(check(symbols('2.4'))[0], 0)
        self.assertEqual(check(symbols('2.34'))[0], 1)

    def test_static_needs_no_symbols(self):
        rc, out = check('', 'ELF 64-bit LSB executable, statically linked')
        self.assertEqual(rc, 0, out)

    def test_unreadable_binary_fails(self):
        self.assertEqual(check('')[0], 1)

    def test_missing_binary_fails(self):
        r = subprocess.run(['bash', CHECK, '/nonexistent/zscan'], capture_output=True, text=True)
        self.assertEqual((r.returncode, 'not built' in r.stdout), (1, True))


if __name__ == '__main__':
    unittest.main()
