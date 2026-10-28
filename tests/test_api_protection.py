"""The web server runs as root: only requests with the session token, from this machine and this page get through."""
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path

SECRET = 'a-long-random-session-token'


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, token=SECRET):
        self.state = tempfile.mkdtemp()
        self.port = free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith('ZFS_SALVAGE_')}
        env.update(ZFS_SALVAGE_STATE_DIR=self.state, ZFS_SALVAGE_CASES_DIR=self.state, 
                   SALVAGE_UI_PORT=str(self.port))
        if token:
            env['ZFS_SALVAGE_TOKEN'] = token
        self.proc = subprocess.Popen([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'web.py')], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                socket.create_connection(('127.0.0.1', self.port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)

    def request(self, method, path, headers=None, body=None, host=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        c.putrequest(method, path, skip_host=True)
        c.putheader('Host', host or f'localhost:{self.port}')
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        data = body.encode() if body is not None else None
        c.putheader('Content-Length', str(len(data or b'')))
        c.endheaders(data)
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.proc.stderr.close()
        fx.rmtree(self.state)


class Protection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def status(self, *a, **kw):
        return self.srv.request(*a, **kw)[0]

    def test_post_without_token_is_forbidden(self):
        # for example: curl -X POST localhost:8765/api/select -d '{"op":"reset"}'
        self.assertEqual(self.status('POST', '/api/select', body='{"op":"reset"}'), 403)
        self.assertEqual(self.status('POST', '/api/transfer/start', body='{}'), 403)

    def test_get_without_token_is_forbidden_too(self):
        for path in ('/', '/api/workflow', '/select', '/transfer', '/steps.js'):
            self.assertEqual(self.status('GET', path), 403, path)

    def test_wrong_token_is_forbidden(self):
        for token in ('', 'x', SECRET[:-1], SECRET + 'x', SECRET.upper()):
            self.assertEqual(self.status('GET', '/api/workflow', {'X-ZS-Token': token}), 403, token)
        self.assertEqual(self.status('GET', '/?t=nope'), 403)

    def test_right_token_in_the_header_works(self):
        self.assertEqual(self.status('GET', '/api/workflow', {'X-ZS-Token': SECRET}), 200)
        # a request that is let through reaches the handler: a bad body there is the handler's own 400
        self.assertEqual(self.status('POST', '/api/target/choose', {'X-ZS-Token': SECRET}, '{}'), 400)

    def test_foreign_origin_is_forbidden_even_with_the_token(self):
        for origin in ('http://evil.example', 'null', f'http://localhost.evil.example:{self.srv.port}', 'https://localhost'):
            h = {'X-ZS-Token': SECRET, 'Origin': origin}
            self.assertEqual(self.status('POST', '/api/target/choose', h, '{}'), 403, origin)
            self.assertEqual(self.status('GET', '/api/workflow', h), 403, origin)

    def test_own_origin_is_fine(self):
        for host in (f'localhost:{self.srv.port}', f'127.0.0.1:{self.srv.port}'):
            h = {'X-ZS-Token': SECRET, 'Origin': f'http://{host}'}
            self.assertEqual(self.status('GET', '/api/workflow', h, host=host), 200, host)

    def test_other_host_names_are_forbidden_dns_rebinding(self):
        for host in ('evil.example', f'evil.example:{self.srv.port}', f'localhost:{self.srv.port + 1}', 'localhost'):
            self.assertEqual(self.status('GET', '/api/workflow', {'X-ZS-Token': SECRET}, host=host), 403, host)

    def test_the_link_sets_a_cookie_and_the_ui_works_with_it(self):
        status, headers, _ = self.srv.request('GET', f'/?t={SECRET}')
        self.assertEqual((status, headers['Location']), (302, '/'))
        cookie = headers['Set-Cookie']
        self.assertIn(f'zs_token_{self.srv.port}=', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        jar = {'Cookie': cookie.split(';')[0]}
        self.assertEqual(self.status('GET', '/', jar), 200)
        self.assertEqual(self.status('GET', '/api/workflow', jar), 200)
        self.assertEqual(self.status('POST', '/api/target/choose', jar, '{}'), 400)
        self.assertEqual(self.status('GET', '/select', jar), 200)

    def test_other_query_parameters_survive_the_redirect(self):
        status, headers, _ = self.srv.request('GET', f'/api/search?t={SECRET}&q=abc')
        self.assertEqual((status, headers['Location']), (302, '/api/search?q=abc'))

    def test_cookie_of_another_port_is_not_accepted(self):
        jar = {'Cookie': f'zs_token_{self.srv.port + 1}={SECRET}'}
        self.assertEqual(self.status('GET', '/api/workflow', jar), 403)


class NoTokenGiven(unittest.TestCase):
    def test_a_server_without_a_configured_token_makes_one_and_prints_the_link(self):
        srv = Server(token=None)
        try:
            self.assertEqual(srv.request('GET', '/api/workflow')[0], 403)
            srv.proc.terminate()
            srv.proc.wait(timeout=10)
            link = next(l for l in srv.proc.stderr.read().splitlines() if '?t=' in l)
            self.assertGreaterEqual(len(link.split('?t=')[1].split()[0]), 20)
        finally:
            srv.proc.stderr.close()
            fx.rmtree(srv.state)


if __name__ == '__main__':
    unittest.main()
