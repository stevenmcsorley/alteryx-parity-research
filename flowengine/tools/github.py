"""GitHub Get Last Commit and GitHub Commit through the REST API; no vendor code or parity claim.

Functional source: the public tool help linked in the contracts. Requests use a saved GitHub
connection (token in the OS vault) and are authorized only under its base URL. Both tools need the
run's network permission. Commit also needs the remote-write permission and creates exactly one
commit per run with the Git Data API. The branch update is not forced, so a concurrent push fails
instead of being overwritten.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from ..errors import ConfigError, DataError, EngineError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema
from ._common import require_fields
from .download import UNBOUNDED_TEXT, _HttpOnlyRedirects

_NAME = re.compile(r'^[A-Za-z0-9_.-]{1,100}$')
_REPO_PROPS = {
    'connection_id': {'type': 'string', 'minLength': 1, 'x-flowbench-connection': 'github',
                      'description': 'Saved GitHub connection (Web & API in the toolbar)'},
    'owner': {'type': 'string', 'minLength': 1},
    'repo': {'type': 'string', 'minLength': 1},
    'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 300, 'default': 30},
}


def _text(name):
    return Field(name, FieldType.V_WSTRING, UNBOUNDED_TEXT)


def _check_repo(cfg):
    for key in ('owner', 'repo'):
        if not _NAME.match(cfg[key]) or cfg[key] in ('.', '..'):
            raise ConfigError(f'GitHub {key} {cfg[key]!r} is not a valid name')
    branch = cfg.get('branch') or ''
    if branch and (branch.startswith('/') or branch.endswith('/') or '..' in branch
                   or any(c in branch for c in ' ~^:?*[\\') or any(ord(c) < 32 for c in branch)):
        raise ConfigError(f'GitHub branch {branch!r} is not a valid branch name')


def _permit(ctx, writes=False):
    if not ctx.limits.get('allow_network'):
        raise EngineError('GitHub tools send network requests and are disabled for this run. '
                          'Allow network requests (CLI --allow-network, or confirm in the desktop app).',
                          code='permission-denied', node_id=ctx.node_id)
    if writes and not ctx.limits.get('allow_remote_writes'):
        raise EngineError('GitHub Commit pushes to a remote repository and is disabled for this run. '
                          'Allow remote writes (CLI --allow-remote-writes, or confirm in the desktop app).',
                          code='permission-denied', node_id=ctx.node_id)


class _Client:
    def __init__(self, cfg):
        from ..service_connections import ServiceConnectionStore
        store = ServiceConnectionStore()
        self.connection = store.get(cfg['connection_id'], kind='github')
        self.secret = store.secret(self.connection)
        self.timeout = cfg['timeout_seconds']
        self.repo = f"/repos/{urllib.parse.quote(cfg['owner'], safe='')}/{urllib.parse.quote(cfg['repo'], safe='')}"
        self.opener = urllib.request.build_opener(_HttpOnlyRedirects(self.connection))

    def call(self, method, path, body=None, query=None):
        url = self.connection.base_url.rstrip('/') + self.repo + path
        if query:
            url += '?' + urllib.parse.urlencode({k: v for k, v in query.items() if v})
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                   'User-Agent': 'Flowbench', **self.connection.authorize(url, self.secret)}
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b'null')
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b'{}').get('message') or e.reason
            except (ValueError, AttributeError):
                detail = e.reason
            raise DataError(f'GitHub API {method} {path or "/"} returned {e.code}: {detail}') from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise DataError(f'GitHub API request failed: {getattr(e, "reason", None) or e}') from None

    def default_branch(self):
        return self.call('GET', '')['default_branch']


def _commit_web_url(connection, cfg, sha):
    """Browser URL of a commit, derived from the API root when the API response omits html_url."""
    parts = urllib.parse.urlsplit(connection.base_url.rstrip('/'))
    host, path = parts.netloc, parts.path
    if host.lower() == 'api.github.com':
        host, path = 'github.com', ''
    elif path.endswith('/api/v3'):
        path = path[:-len('/api/v3')]
    else:
        return None  # unknown API layout: do not guess a URL
    owner, repo = (urllib.parse.quote(cfg[k], safe='') for k in ('owner', 'repo'))
    return urllib.parse.urlunsplit((parts.scheme, host, f'{path}/{owner}/{repo}/commit/{sha}', '', ''))


def _ref(branch):
    return '/git/ref/heads/' + urllib.parse.quote(branch, safe='/')


LAST_COMMIT_SCHEMA = Schema([_text('Repository'), _text('Branch'), _text('Path'), _text('SHA'), _text('Author'),
                             _text('AuthorEmail'), Field('CommitDate', FieldType.DATETIME), _text('Message'),
                             _text('URL')])


class GitHubLastCommit(Tool):
    manifest = ToolManifest(
        tool_id='core.github_last_commit', version='1.0.0', display_name='GitHub Get Last Commit',
        catalog_ids=('developer:github-get-last-commit',), inputs=(), outputs=(Anchor('output', 'Output'),),
        side_effects='network', deterministic=False,
        description='Return the latest commit on a branch (the default branch when blank), optionally the latest '
                    'that touched path. Uses a saved GitHub connection; requires permission to make network requests.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['connection_id', 'owner', 'repo'],
                       'properties': {**_REPO_PROPS, 'branch': {'type': 'string', 'default': ''},
                                      'path': {'type': 'string', 'default': '',
                                               'description': 'Only commits that changed this file or folder'}}})

    def check_config(self, cfg):
        _check_repo(cfg)

    def infer(self, ctx, cfg, inputs):
        return {'output': LAST_COMMIT_SCHEMA}

    def run(self, ctx, cfg, inputs):
        _permit(ctx)
        client = _Client(cfg)
        branch = cfg['branch'] or client.default_branch()
        commits = client.call('GET', '/commits', query={'sha': branch, 'path': cfg['path'], 'per_page': '1'})
        if not commits:
            raise DataError(f"No commits found on {branch!r}" + (f" for path {cfg['path']!r}" if cfg['path'] else ''))
        latest = commits[0]
        author = latest['commit'].get('author') or {}
        when = author.get('date')
        stamp = (_dt.datetime.fromisoformat(when.replace('Z', '+00:00')).astimezone(_dt.timezone.utc)
                 .replace(tzinfo=None) if when else None)
        row = (f"{cfg['owner']}/{cfg['repo']}", branch, cfg['path'] or None, latest['sha'], author.get('name'),
               author.get('email'), stamp, latest['commit'].get('message'), latest.get('html_url'))
        return {'output': Table.from_rows(LAST_COMMIT_SCHEMA, [row])}


class GitHubCommit(Tool):
    manifest = ToolManifest(
        tool_id='core.github_commit', version='1.0.0', display_name='GitHub Commit',
        catalog_ids=('developer:github-commit',),
        inputs=(Anchor('input', 'Input', description='One file per record: repository path and content'),),
        outputs=(Anchor('output', 'Output', description='Input records with BlobSHA, CommitSHA and CommitURL'),),
        side_effects='network', deterministic=False,
        description='Commit every input record as a file (path_field, content_field) to an existing branch in one '
                    'commit with the given message. The branch is not force-updated. Uses a saved GitHub connection; '
                    'requires permission for network requests and remote writes.',
        config_schema={'type': 'object', 'additionalProperties': False,
                       'required': ['connection_id', 'owner', 'repo', 'branch', 'path_field', 'content_field', 'message'],
                       'properties': {**_REPO_PROPS, 'branch': {'type': 'string', 'minLength': 1},
                                      'path_field': {'type': 'string', 'minLength': 1},
                                      'content_field': {'type': 'string', 'minLength': 1},
                                      'message': {'type': 'string', 'minLength': 1, 'maxLength': 10000}}})

    def check_config(self, cfg):
        _check_repo(cfg)

    def infer(self, ctx, cfg, inputs):
        schema = inputs['input']
        require_fields(schema, [cfg['path_field'], cfg['content_field']])
        if not schema.field(cfg['path_field']).type.is_string:
            raise SchemaError(f"GitHub Commit path_field {cfg['path_field']!r} must be a text field")
        clash = [n for n in ('BlobSHA', 'CommitSHA', 'CommitURL') if n in schema]
        if clash:
            raise SchemaError(f'GitHub Commit output field(s) {clash} already exist in the input')
        return {'output': Schema([*schema, _text('BlobSHA'), _text('CommitSHA'), _text('CommitURL')])}

    @staticmethod
    def _files(cfg, table):
        files, seen = [], set()
        for number, record in enumerate(table.to_dicts(), start=1):
            path, content = record[cfg['path_field']], record[cfg['content_field']]
            parts = (path or '').split('/')
            if not path or path.startswith('/') or '\\' in path or any(p in ('', '.', '..') for p in parts) \
                    or any(ord(c) < 32 for c in path):
                raise DataError(f'GitHub Commit record {number} has an invalid repository path {path!r}')
            if path in seen:
                raise DataError(f'GitHub Commit record {number} repeats the path {path!r}')
            if content is None:
                raise DataError(f'GitHub Commit record {number} has no content')
            seen.add(path)
            raw = content if isinstance(content, bytes) else str(content).encode('utf-8')
            files.append((path, raw))
        return files

    def run(self, ctx, cfg, inputs):
        _permit(ctx, writes=True)
        table: Table = inputs['input']
        schema = self.infer(ctx, cfg, {'input': table.schema})['output']
        files = self._files(cfg, table)
        if not files:
            ctx.info('GitHub Commit received no records; nothing was committed')
            return {'output': Table.empty(schema)}
        client = _Client(cfg)
        parent = client.call('GET', _ref(cfg['branch']))['object']['sha']
        base_tree = client.call('GET', f'/git/commits/{parent}')['tree']['sha']
        blobs = []
        for path, raw in files:
            ctx.check_cancelled()
            blobs.append(client.call('POST', '/git/blobs', {'content': base64.b64encode(raw).decode('ascii'),
                                                            'encoding': 'base64'})['sha'])
        tree = client.call('POST', '/git/trees', {'base_tree': base_tree, 'tree': [
            {'path': path, 'mode': '100644', 'type': 'blob', 'sha': sha} for (path, _), sha in zip(files, blobs)]})['sha']
        commit = client.call('POST', '/git/commits', {'message': cfg['message'], 'tree': tree, 'parents': [parent]})
        ctx.check_cancelled()
        client.call('PATCH', '/git/refs/heads/' + urllib.parse.quote(cfg['branch'], safe='/'),
                    {'sha': commit['sha'], 'force': False})
        ctx.info(f"GitHub Commit pushed {len(files)} file(s) to {cfg['owner']}/{cfg['repo']}:{cfg['branch']}",
                 commit=commit['sha'])
        url = commit.get('html_url') or _commit_web_url(client.connection, cfg, commit['sha'])
        return {'output': Table.from_rows(schema, [(*row, sha, commit['sha'], url)
                                                   for row, sha in zip(table.rows(), blobs)])}


TOOLS = [GitHubLastCommit, GitHubCommit]
