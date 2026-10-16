"""GitHub Get Last Commit and GitHub Commit against a fake GitHub REST API (no external network)."""
import base64
import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import flowengine.db.vault as vault_module
from flowengine.errors import WorkflowValidationError
from flowengine.executor import run_workflow
from flowengine.service_connections import ServiceConnectionStore
from flowengine.workflow_effects import workflow_effects

from helpers import ROOT, edge, text_input, workflow
from test_service_connections import MemoryVault

NET = {'allow_network': True}
WRITE = {'allow_network': True, 'allow_remote_writes': True}


def sha(*parts):
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()


class FakeGitHub:
    """Tiny in-memory model of one repository: refs, commits, trees and blobs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.blobs, self.trees, self.commits = {}, {}, {}
        self.auth = []
        self.race = False
        root = self.tree(None, {'README.md': sha('readme')})
        first = self.commit('Initial commit', root, [], '2026-10-01T09:00:00Z', ['README.md'])
        docs = self.tree(root, {'docs/guide.md': sha('guide')})
        second = self.commit('Add guide', docs, [first], '2026-10-02T10:30:00+01:00', ['docs/guide.md'])
        self.refs = {'main': second, 'release': first}

    def tree(self, base, entries):
        files = dict(self.trees[base]['files']) if base else {}
        files.update(entries)
        key = sha('tree', files)
        self.trees[key] = {'files': files}
        return key

    def commit(self, message, tree, parents, date='2026-10-16T12:00:00Z', paths=()):
        key = sha('commit', message, tree, parents, date)
        self.commits[key] = {'message': message, 'tree': tree, 'parents': parents, 'date': date, 'paths': list(paths)}
        return key

    def history(self, head):
        while head:
            yield head
            parents = self.commits[head]['parents']
            head = parents[0] if parents else None


class Handler(BaseHTTPRequestHandler):
    gh: FakeGitHub = None

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_api(self):
        gh, parts = self.gh, urlsplit(self.path)
        gh.auth.append(self.headers.get('Authorization'))
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'null')
        prefix = '/api/v3/repos/octo/data'
        if not parts.path.startswith(prefix):
            return self.reply(404, {'message': 'Not Found'})
        route, query = parts.path[len(prefix):], parse_qs(parts.query)
        with gh.lock:
            if self.command == 'GET' and route == '':
                return self.reply(200, {'default_branch': 'main'})
            if self.command == 'GET' and route == '/commits':
                head = gh.refs.get(query.get('sha', ['main'])[0])
                if head is None:
                    return self.reply(404, {'message': 'No commit found for SHA'})
                path = query.get('path', [''])[0]
                found = [c for c in gh.history(head) if not path or any(p.startswith(path) for p in gh.commits[c]['paths'])]
                return self.reply(200, [{'sha': c, 'html_url': f'https://github.test/octo/data/commit/{c}',
                                         'commit': {'message': gh.commits[c]['message'],
                                                    'author': {'name': 'Ann', 'email': 'ann@example.com',
                                                               'date': gh.commits[c]['date']}}}
                                        for c in found[:int(query.get('per_page', ['30'])[0])]])
            if self.command == 'GET' and route.startswith('/git/ref/heads/'):
                name = route[len('/git/ref/heads/'):]
                if name not in gh.refs:
                    return self.reply(404, {'message': 'Not Found'})
                head = gh.refs[name]
                if gh.race:  # someone else pushes right after we read the branch
                    gh.refs[name] = gh.commit('Concurrent push', gh.commits[head]['tree'], [head])
                return self.reply(200, {'object': {'sha': head}})
            if self.command == 'GET' and route.startswith('/git/commits/'):
                return self.reply(200, {'tree': {'sha': gh.commits[route.rsplit('/', 1)[1]]['tree']}})
            if self.command == 'POST' and route == '/git/blobs':
                key = sha('blob', body['content'])
                gh.blobs[key] = base64.b64decode(body['content'])
                return self.reply(201, {'sha': key})
            if self.command == 'POST' and route == '/git/trees':
                return self.reply(201, {'sha': gh.tree(body['base_tree'], {e['path']: e['sha'] for e in body['tree']})})
            if self.command == 'POST' and route == '/git/commits':
                key = gh.commit(body['message'], body['tree'], body['parents'],
                                paths=[e for e in gh.trees[body['tree']]['files']])
                return self.reply(201, {'sha': key, 'html_url': f'https://github.test/octo/data/commit/{key}'})
            if self.command == 'PATCH' and route.startswith('/git/refs/heads/'):
                name = route[len('/git/refs/heads/'):]
                if gh.commits[body['sha']]['parents'] != [gh.refs[name]] and not body.get('force'):
                    return self.reply(422, {'message': 'Update is not a fast forward'})
                gh.refs[name] = body['sha']
                return self.reply(200, {'object': {'sha': body['sha']}})
        return self.reply(404, {'message': 'Not Found'})

    do_GET = do_POST = do_PATCH = handle_api


class GitHubToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.api = f'http://127.0.0.1:{cls.server.server_address[1]}/api/v3'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.gh = self.gh = FakeGitHub()
        self._home = tempfile.TemporaryDirectory()
        self._patches = [mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name}),
                         mock.patch.object(vault_module, '_VAULT', MemoryVault())]
        for p in self._patches:
            p.start()
        store = ServiceConnectionStore()
        self.conn = store.save({'name': 'GitHub Enterprise', 'kind': 'github', 'base_url': self.api}, secret='ghp_test').id
        self.web = store.save({'name': 'Other', 'kind': 'web', 'base_url': self.api}, secret='x').id

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._home.cleanup()

    def last_commit(self, limits=NET, **config):
        doc = workflow([{'id': 'gh', 'tool': 'core.github_last_commit',
                         'config': {'connection_id': self.conn, 'owner': 'octo', 'repo': 'data', **config}}], [])
        return run_workflow(doc, base_dir=ROOT, limits=limits)

    def commit(self, files, limits=WRITE, **config):
        doc = workflow([text_input('t', [('path', 'v_wstring'), ('body', 'v_wstring')], files),
                        {'id': 'gh', 'tool': 'core.github_commit',
                         'config': {'connection_id': self.conn, 'owner': 'octo', 'repo': 'data', 'branch': 'main',
                                    'path_field': 'path', 'content_field': 'body', 'message': 'Update data', **config}}],
                       [edge('t', 'output', 'gh', 'input')])
        return run_workflow(doc, base_dir=ROOT, limits=limits)

    def test_last_commit_on_default_branch_branch_and_path(self):
        result = self.last_commit()
        self.assertTrue(result.ok, result.error)
        row = result.output('gh').to_dicts()[0]
        self.assertEqual((row['Repository'], row['Branch'], row['Message']), ('octo/data', 'main', 'Add guide'))
        self.assertEqual(str(row['CommitDate']), '2026-10-02 09:30:00')  # converted to UTC
        self.assertEqual(row['SHA'], self.gh.refs['main'])
        self.assertEqual(self.last_commit(branch='release').output('gh').column('Message'), ['Initial commit'])
        self.assertEqual(self.last_commit(path='README.md').output('gh').column('Message'), ['Initial commit'])
        self.assertIn('No commits found', self.last_commit(path='nothing-here').error['message'])
        self.assertEqual(set(self.gh.auth), {'Bearer ghp_test'})

    def test_commit_pushes_one_atomic_commit(self):
        before = self.gh.refs['main']
        result = self.commit([['data/a.csv', 'x,y\n1,2\n'], ['data/b.txt', 'hello']])
        self.assertTrue(result.ok, result.error)
        out = result.output('gh')
        head = self.gh.refs['main']
        self.assertEqual(set(out.column('CommitSHA')), {head})
        self.assertEqual(self.gh.commits[head]['parents'], [before])
        self.assertEqual(self.gh.commits[head]['message'], 'Update data')
        files = self.gh.trees[self.gh.commits[head]['tree']]['files']
        self.assertEqual(self.gh.blobs[files['data/a.csv']], b'x,y\n1,2\n')
        self.assertIn('README.md', files)  # existing files are kept
        self.assertEqual(out.column('BlobSHA'), [files['data/a.csv'], files['data/b.txt']])
        self.assertEqual(len(self.commit([]).output('gh')), 0)
        self.assertEqual(self.gh.refs['main'], head)  # no records -> no commit

    def test_commit_refuses_to_overwrite_a_concurrent_push(self):
        self.gh.race = True
        result = self.commit([['a.txt', 'mine']])
        self.assertEqual(result.status, 'failed')
        self.assertIn('not a fast forward', result.error['message'])
        self.assertEqual(self.gh.commits[self.gh.refs['main']]['message'], 'Concurrent push')

    def test_permissions_paths_and_connection_kind(self):
        self.assertEqual(self.last_commit(limits={}).error['code'], 'permission-denied')
        denied = self.commit([['a.txt', 'x']], limits=NET)
        self.assertEqual(denied.error['code'], 'permission-denied')
        self.assertIn('remote writes', denied.error['message'])
        self.assertEqual(len(self.gh.auth), 0)
        for files, message in [([['../escape.txt', 'x']], 'invalid repository path'),
                               ([['/abs.txt', 'x']], 'invalid repository path'),
                               ([['a.txt', 'x'], ['a.txt', 'y']], 'repeats the path'),
                               ([['a.txt', None]], 'no content')]:
            with self.subTest(message=message):
                self.assertIn(message, self.commit(files).error['message'])
        self.assertIn('Unknown github connection', self.last_commit(connection_id=self.web).error['message'])
        with self.assertRaisesRegex(WorkflowValidationError, "owner '../x' is not a valid name"):
            self.last_commit(owner='../x')
        with self.assertRaisesRegex(WorkflowValidationError, 'not a valid branch name'):
            self.last_commit(branch='main/../x')
        doc = workflow([text_input('t', [('path', 'v_wstring'), ('body', 'v_wstring')], []),
                        {'id': 'gh', 'tool': 'core.github_commit', 'config': {}}], [])
        effects = workflow_effects(doc, ROOT)
        self.assertEqual([e['node_id'] for e in effects['remote_writes']], ['gh'])
        self.assertEqual([e['node_id'] for e in effects['network_requests']], ['gh'])


if __name__ == '__main__':
    unittest.main()
