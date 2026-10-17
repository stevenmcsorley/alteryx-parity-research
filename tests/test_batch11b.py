"""Batch 11b: download-on-first-use components, and the spaCy and zero-shot tools' plumbing.

The real language libraries and models are exercised by tests/integration/test_text_addons_integration.py,
which runs only where the components are installed, as in the CI job that installs them.
"""
import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import flowengine.addons as addons
from flowengine.errors import DataError, EngineError
from flowengine.executor import run_workflow
from flowengine.workflow_effects import workflow_effects

from helpers import ROOT, edge, run_tool_result, text_input, workflow

F = [('text', 'v_wstring')]


class HomeTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self.home.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.home.cleanup()

    def mark_installed(self, *components):
        """Pretend components are installed (marker plus an add-on interpreter / model files)."""
        exe = addons.env_python()
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text('')
        for c in components:
            if c == 'zero-shot-model':
                for name in addons.ZERO_SHOT['files']:
                    (addons.zero_shot_dir() / name).parent.mkdir(parents=True, exist_ok=True)
                    (addons.zero_shot_dir() / name).write_text('x')
            addons._marker(c).parent.mkdir(parents=True, exist_ok=True)
            addons._marker(c).write_text('{}')


class ComponentTests(HomeTestCase):
    def test_requirements_expand_and_missing_lists_only_absent(self):
        self.assertEqual(addons.with_requirements(['spacy-model:french']), ['language-pack', 'spacy-model:french'])
        self.assertEqual([c['id'] for c in addons.missing(['zero-shot-model'])], ['neural-runtime', 'zero-shot-model'])
        self.mark_installed('neural-runtime')
        self.assertEqual([c['id'] for c in addons.missing(['zero-shot-model'])], ['zero-shot-model'])
        with self.assertRaises(EngineError) as e:
            addons.require(['zero-shot-model'], 'Zero-shot')
        self.assertEqual(e.exception.code, 'addon-missing')
        self.assertIn('python -m flowengine addons install zero-shot-model', e.exception.message)

    def test_install_runs_pinned_binary_pip_and_verifies_downloads(self):
        calls = []
        payload = b'model bytes'
        good = hashlib.sha256(payload).hexdigest()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_address[1]}/m.whl'
        try:
            with tempfile.TemporaryDirectory() as d:
                target = Path(d) / 'm.whl'
                addons._download(url, target, len(payload), good)
                self.assertEqual(target.read_bytes(), payload)
                with self.assertRaises(DataError):
                    addons._download(url, Path(d) / 'bad.whl', len(payload), '0' * 64)
                self.assertFalse((Path(d) / 'bad.whl').exists())
        finally:
            server.shutdown()
            server.server_close()

        def fake_env(log):
            self.mark_installed()

        def fake_download(url, target, size, sha):
            calls.append(('download', url, sha))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'x')
        with mock.patch.object(addons, '_ensure_env', fake_env), \
                mock.patch.object(addons, '_pip', lambda args, log: calls.append(('pip', tuple(args)))), \
                mock.patch.object(addons, '_download', fake_download):
            done = addons.install(['spacy-model:english'])
        self.assertEqual(done, ['language-pack', 'spacy-model:english'])
        pip = [c for c in calls if c[0] == 'pip']
        self.assertIn('spacy==3.8.16', pip[0][1])
        self.assertEqual(pip[0][1][0], '-c')
        self.assertIn('spacy==3.8.16', Path(pip[0][1][1]).read_text())
        download = next(c for c in calls if c[0] == 'download')
        self.assertTrue(download[1].startswith('https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/'))
        self.assertEqual(download[2], addons.SPACY_MODELS['english'][2])
        self.assertTrue(addons.installed('spacy-model:english'))
        self.assertEqual(addons.install(['spacy-model:english']), [])  # already there

    def test_preflight_lists_downloads_once(self):
        doc = workflow([{'id': 'n', 'tool': 'core.named_entity_recognition', 'config': {'text_field': 'text', 'language': 'german'}},
                        {'id': 'p', 'tool': 'core.part_of_speech_tagger', 'config': {'text_field': 'text', 'language': 'german'}},
                        {'id': 'z', 'tool': 'core.zero_shot_classification', 'config': {'text_field': 't', 'label_field': 'l'}},
                        {'id': 'pp', 'tool': 'core.text_preprocessing', 'config': {'text_field': 'text'}}], [])
        found = workflow_effects(doc, ROOT)
        self.assertEqual([d['id'] for d in found['downloads']],
                         ['language-pack', 'spacy-model:german', 'neural-runtime', 'zero-shot-model'])
        self.assertEqual(found['downloads'][1]['nodes'], ['n', 'p'])
        self.mark_installed('language-pack', 'spacy-model:german', 'neural-runtime', 'zero-shot-model')
        self.assertEqual(workflow_effects(doc, ROOT)['downloads'], [])

    def test_tools_stop_with_addon_missing(self):
        r = run_tool_result('core.part_of_speech_tagger', {'text_field': 'text'}, {'input': (F, [['A cat.']])})
        self.assertEqual(r.error['code'], 'addon-missing')


class ToolPlumbingTests(HomeTestCase):
    """Tool tables around canned worker replies; the workers themselves run in the integration test."""

    def run_with(self, replies, nodes, edges):
        seen = []

        def fake(ctx, script, request, timeout=1800):
            seen.append((script, request))
            return {'results': replies(request)}
        with mock.patch.object(addons, 'run_worker', fake):
            return run_workflow(workflow(nodes, edges), base_dir=ROOT), seen

    def test_ner_custom_entities_and_predict(self):
        self.mark_installed('language-pack', 'spacy-model:english')
        reply = lambda req: [{'entities': [{'entity': 'Riesling', 'label': 'GRAPE', 'character_index': 9, 'word_index': 2,
                                            'entity_length': 8}], 'diagram': '<div>x</div>'} for _ in req['texts']]
        nodes = [text_input('d', F, [['We drank Riesling.'], [None]]), text_input('e', [('Entity', 'v_wstring'), ('Label', 'v_wstring')],
                                                                              [['riesling', 'grape']]),
                 {'id': 'n', 'tool': 'core.named_entity_recognition', 'config': {'text_field': 'text', 'train_new_entities': True}},
                 {'id': 'p', 'tool': 'core.ml_predict', 'config': {}}]
        edges = [edge('d', 'output', 'n', 'input'), edge('e', 'output', 'n', 'entities'), edge('n', 'model', 'p', 'model'),
                 edge('d', 'output', 'p', 'input')]
        r, seen = self.run_with(reply, nodes, edges)
        self.assertTrue(r.ok, r.error)
        out = r.output('n').to_dicts()
        self.assertEqual(json.loads(out[0]['entities'])[0]['label'], 'GRAPE')
        self.assertIsNone(out[1]['entities'])
        self.assertEqual(seen[0][1]['patterns'], [{'entity': 'riesling', 'label': 'GRAPE'}])
        self.assertEqual(seen[0][1]['model'], 'en_core_web_sm')
        self.assertEqual(r.output('p').column('entities'), r.output('n').column('entities'))

    def test_preprocessing_merges_stop_word_sources(self):
        self.mark_installed('language-pack', 'spacy-model:spanish')
        nodes = [text_input('d', F, [['Los perros corren']]), text_input('s', [('word', 'v_wstring')], [['perro'], [None]]),
                 {'id': 't', 'tool': 'core.text_preprocessing', 'config': {'text_field': 'text', 'language': 'spanish',
                                                                            'lemmatize': True, 'custom_stopwords': 'gato, raton'}}]
        r, seen = self.run_with(lambda req: ['perro correr'], nodes, [edge('d', 'output', 't', 'input'), edge('s', 'output', 't', 'stopwords')])
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.output('t').column('text_processed'), ['perro correr'])
        self.assertEqual(seen[0][1]['custom_stopwords'], ['gato', 'raton', 'perro'])
        self.assertEqual(seen[0][1]['model'], 'es_core_news_sm')

    def test_zero_shot_columns_and_top_label(self):
        self.mark_installed('neural-runtime', 'zero-shot-model')
        nodes = [text_input('d', F, [['The striker scored'], ['']]), text_input('l', [('label', 'v_wstring')], [['sports'], ['finance'], ['sports']]),
                 {'id': 'z', 'tool': 'core.zero_shot_classification', 'config': {'text_field': 'text', 'label_field': 'label'}}]
        r, seen = self.run_with(lambda req: [[0.9, 0.1] for _ in req['texts']], nodes,
                                [edge('d', 'output', 'z', 'input'), edge('l', 'output', 'z', 'labels')])
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.output('z').to_dicts(), [{'text': 'The striker scored', 'sports': 0.9, 'finance': 0.1, 'top_label': 'sports'},
                                                    {'text': '', 'sports': None, 'finance': None, 'top_label': None}])
        self.assertEqual(seen[0][1]['labels'], ['sports', 'finance'])
        self.assertEqual(seen[0][1]['texts'], ['The striker scored'])


if __name__ == '__main__':
    unittest.main()
