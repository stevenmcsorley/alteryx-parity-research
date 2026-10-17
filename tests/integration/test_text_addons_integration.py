"""Text tools on the real downloaded components: spaCy 3.8 with its small models, and the ONNX zero-shot model.

Runs only where the components are installed (``python -m flowengine addons install spacy-model:english
spacy-model:german zero-shot-model``), as in the ``text-addons`` CI job.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowengine import addons  # noqa: E402
from flowengine.executor import run_workflow  # noqa: E402

from helpers import ROOT, edge, text_input, workflow  # noqa: E402

READY = all(addons.installed(c) for c in ('language-pack', 'spacy-model:english', 'spacy-model:german',
                                          'neural-runtime', 'zero-shot-model'))
F = [{'name': 'text', 'type': 'v_wstring', 'size': 2000}]


def run(nodes, edges):
    r = run_workflow(workflow(nodes, edges), base_dir=ROOT)
    assert r.ok, r.error
    return r


@unittest.skipUnless(READY, 'text add-on components are not installed')
class TextAddonsIntegrationTests(unittest.TestCase):
    def test_named_entities_with_custom_terms(self):
        r = run([text_input('d', F, [['Apple opened an office in London on Monday.'], ['We drank a Riesling from the Mosel.']]),
                 text_input('e', [('Entity', 'v_wstring'), ('Label', 'v_wstring')], [['riesling', 'GRAPE']]),
                 {'id': 'n', 'tool': 'core.named_entity_recognition', 'config': {'text_field': 'text', 'train_new_entities': True}}],
                [edge('d', 'output', 'n', 'input'), edge('e', 'output', 'n', 'entities')])
        first, second = (json.loads(v) for v in r.output('n').column('entities'))
        labels = {e['entity']: e['label'] for e in first}
        self.assertEqual(labels.get('Apple'), 'ORG')
        self.assertEqual(labels.get('London'), 'GPE')
        self.assertEqual(labels.get('Monday'), 'DATE')
        london = next(e for e in first if e['entity'] == 'London')
        self.assertEqual((london['character_index'], london['word_index'], london['entity_length']), (26, 5, 6))
        self.assertIn({'entity': 'Riesling', 'label': 'GRAPE', 'character_index': 11, 'word_index': 3, 'entity_length': 8}, second)
        self.assertIn('<mark', r.output('n').column('entity_diagram')[0])

    def test_part_of_speech_and_german(self):
        r = run([text_input('d', F, [['The cats sat on the mat.']]), text_input('g', F, [['Die Katzen schlafen.']]),
                 {'id': 'p', 'tool': 'core.part_of_speech_tagger', 'config': {'text_field': 'text'}},
                 {'id': 'pg', 'tool': 'core.part_of_speech_tagger', 'config': {'text_field': 'text', 'language': 'german'}}],
                [edge('d', 'output', 'p', 'input'), edge('g', 'output', 'pg', 'input')])
        tags = {t['text']: t for t in json.loads(r.output('p').column('part_of_speech_tags')[0])}
        self.assertEqual((tags['cats']['part_of_speech'], tags['cats']['fine_grained_tag']), ('NOUN', 'NNS'))
        self.assertEqual(tags['cats']['dependency'], 'nsubj')
        self.assertEqual(tags['sat']['part_of_speech_description'], 'verb')
        german = {t['text']: t['part_of_speech'] for t in json.loads(r.output('pg').column('part_of_speech_tags')[0])}
        self.assertEqual(german['Katzen'], 'NOUN')
        self.assertTrue(r.output('p').column('dependency_diagram')[0].startswith('<svg'))

    def test_preprocessing_lemmatizes_and_filters(self):
        r = run([text_input('d', F, [['The runners were running 3 miles!']]),
                 {'id': 't', 'tool': 'core.text_preprocessing', 'config': {'text_field': 'text', 'lemmatize': True}},
                 {'id': 'raw', 'tool': 'core.text_preprocessing', 'config': {'text_field': 'text', 'filter_stopwords': False}}],
                [edge('d', 'output', 't', 'input'), edge('d', 'output', 'raw', 'input')])
        self.assertEqual(r.output('t').column('text_processed'), ['runner run mile'])
        self.assertEqual(r.output('raw').column('text_processed'), ['The runners were running miles'])

    def test_zero_shot(self):
        r = run([text_input('d', F, [['The striker scored twice in the cup final.'], ['Interest rates rose again this quarter.']]),
                 text_input('l', [('label', 'v_wstring')], [['sports'], ['finance'], ['cooking']]),
                 {'id': 'z', 'tool': 'core.zero_shot_classification', 'config': {'text_field': 'text', 'label_field': 'label'}},
                 {'id': 'm', 'tool': 'core.zero_shot_classification', 'config': {'text_field': 'text', 'label_field': 'label',
                                                                                  'multi_label': True}}],
                [edge('d', 'output', 'z', 'input'), edge('l', 'output', 'z', 'labels'),
                 edge('d', 'output', 'm', 'input'), edge('l', 'output', 'm', 'labels')])
        rows = r.output('z').to_dicts()
        self.assertEqual([x['top_label'] for x in rows], ['sports', 'finance'])
        for x in rows:
            self.assertAlmostEqual(x['sports'] + x['finance'] + x['cooking'], 1.0, places=5)
        multi = r.output('m').to_dicts()
        self.assertGreater(multi[0]['sports'], 0.9)
        self.assertLess(multi[0]['cooking'], 0.2)


if __name__ == '__main__':
    unittest.main()
