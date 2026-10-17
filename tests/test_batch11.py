"""Batch 11a: Text Mining tools checked against their reference libraries and known answers."""
import io
import json
import unittest
from collections import Counter

import numpy as np

from flowengine.executor import run_workflow

from helpers import ROOT, edge, run_tool, run_tool_result, text_input, workflow

F = [('text', 'v_wstring')]
TOPICS = (["The battery life of this laptop is amazing and the laptop screen is bright",
           "My laptop battery died, the laptop charger and battery are broken",
           "Pasta and pizza at the restaurant were delicious, great restaurant food",
           "The restaurant served cold pizza and the pasta sauce was bland",
           "The football team won the match after a late football goal",
           "A football match with two goals and a great team performance"] * 6)
TRUTH = ['tech', 'tech', 'food', 'food', 'sport', 'sport'] * 6


class SentimentTests(unittest.TestCase):
    def test_matches_vader_and_categorises(self):
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        vader = SentimentIntensityAnalyzer()
        texts = ['I LOVE this product!!!', 'This is not good at all.', 'The box is brown.', None]
        out, _ = run_tool('core.sentiment_analysis', {'text_field': 'text', 'categorical': True}, {'input': (F, [[t] for t in texts])})
        rows = out['output'].to_dicts()
        for row, text in zip(rows[:3], texts):
            expected = vader.polarity_scores(text)
            self.assertAlmostEqual(row['compound_sentiment_score'], expected['compound'])
            self.assertAlmostEqual(row['negative_sentiment'] + row['neutral_sentiment'] + row['positive_sentiment'], 1.0, places=2)
        self.assertEqual([r['sentiment_category'] for r in rows], ['positive', 'negative', 'neutral', None])

    def test_sentence_level_averages_sentences(self):
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        vader = SentimentIntensityAnalyzer()
        text = 'The food was wonderful. The service was terrible! The room was fine.'
        out, _ = run_tool('core.sentiment_analysis', {'text_field': 'text', 'sentence_level': True}, {'input': (F, [[text]])})
        parts = ['The food was wonderful.', 'The service was terrible!', 'The room was fine.']
        expected = np.mean([vader.polarity_scores(p)['compound'] for p in parts])
        self.assertAlmostEqual(out['output'].to_dicts()[0]['compound_sentiment_score'], expected)


class SummaryTests(unittest.TestCase):
    TEXT = ('Flowbench is a desktop workflow designer. Workflows connect tools that read, change and write data. '
            'The engine that runs workflows is written in Python. The desktop workflow designer shows results under '
            'the canvas. Charts open in a viewer that can zoom. Workflows run locally on the desktop.')

    def test_top_sentences_in_importance_order(self):
        from summa import summarizer
        ranked = summarizer.summarize(self.TEXT, ratio=1.0, split=True, scores=True)
        best = [s for s, _ in sorted(ranked, key=lambda p: -p[1])[:2]]
        long_field = [{'name': 'text', 'type': 'v_wstring', 'size': 4000}]
        out, r = run_tool('core.text_summary', {'text_field': 'text', 'sentences': 2},
                          {'input': (long_field, [[self.TEXT], ['Only one.']])})
        rows = out['output'].to_dicts()
        self.assertEqual(rows[0]['summary'], ' '.join(best))
        self.assertEqual(rows[1]['summary'], 'Only one.')
        self.assertTrue(any('returned whole' in w['message'] for w in r.warnings()))


class TopicModelingTests(unittest.TestCase):
    def test_recovers_themes_and_predict_reproduces_shares(self):
        nodes = [text_input('d', F, [[t] for t in TOPICS]),
                 {'id': 'tm', 'tool': 'core.topic_modeling', 'config': {'text_field': 'text', 'topics': 3, 'top_words': 4,
                                                                         'report': 'word_relevance', 'min_frequency': 1}},
                 {'id': 'p', 'tool': 'core.ml_predict', 'config': {}}]
        edges = [edge('d', 'output', 'tm', 'input'), edge('tm', 'model', 'p', 'model'), edge('d', 'output', 'p', 'input')]
        r = run_workflow(workflow(nodes, edges), base_dir=ROOT)
        self.assertTrue(r.ok, r.error)
        rows = r.output('tm').to_dicts()
        shares = np.array([[row[f'topic_{i}'] for i in (1, 2, 3)] for row in rows])
        np.testing.assert_allclose(shares.sum(axis=1), 1.0)
        dominant = shares.argmax(axis=1)
        for theme in ('tech', 'food', 'sport'):  # every theme maps to one topic
            self.assertEqual(len({d for d, t in zip(dominant, TRUTH) if t == theme}), 1)
        self.assertEqual(len(set(dominant)), 3)
        predicted = np.array([[row[f'topic_{i}'] for i in (1, 2, 3)] for row in r.output('p').to_dicts()])
        np.testing.assert_allclose(predicted, shares, atol=1e-6)
        words = r.output('tm', 'report').to_dicts()
        self.assertEqual(len(words), 12)
        top = {w['Topic']: w['Word'] for w in words if w['Rank'] == 1}
        self.assertEqual({top[d + 1] for d in set(dominant)} & {'laptop', 'battery', 'restaurant', 'pasta', 'pizza', 'football'},
                         set(top.values()))
        for w in words:
            self.assertAlmostEqual(w['Relevance'], 0.6 * np.log(w['Probability']) + 0.4 * np.log(w['Lift']))

    def test_chart_report(self):
        out, _ = run_tool('core.topic_modeling', {'text_field': 'text', 'topics': 3}, {'input': (F, [[t] for t in TOPICS])})
        svg = out['report'].column('Chart')[0]
        self.assertTrue(svg.startswith('<svg') and 'Intertopic distance map' in svg)


class WordCloudTests(unittest.TestCase):
    def test_layout_is_deterministic_without_overlaps(self):
        from flowengine.tools.text_mining import layout_words
        counts = Counter({f'word{i}': 100 - i for i in range(60)})
        a = layout_words(counts, 600, 300, 60, seed=3)
        self.assertEqual(a, layout_words(counts, 600, 300, 60, seed=3))
        self.assertGreater(len(a), 40)
        sizes = [p[1] for p in a]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        for i, p in enumerate(a):
            self.assertTrue(0 <= p[2] and p[2] + p[4] <= 600 and 0 <= p[3] and p[3] + p[5] <= 300)
            for q in a[i + 1:]:
                overlap = p[2] < q[2] + q[4] and q[2] < p[2] + p[4] and p[3] < q[3] + q[5] and q[3] < p[3] + p[5]
                self.assertFalse(overlap, (p, q))

    def test_mask_limits_words_to_its_dark_area(self):
        from PIL import Image
        from flowengine.tools.text_mining import _mask_grid, layout_words
        img = Image.new('L', (400, 200), 255)
        img.paste(0, (200, 0, 400, 200))  # right half dark
        buf = io.BytesIO()
        img.save(buf, 'PNG')
        mask = _mask_grid(buf.getvalue(), 400, 200)
        placed = layout_words(Counter({f'w{i}': 50 - i for i in range(30)}), 400, 200, 30, mask=mask)
        self.assertTrue(placed)
        self.assertTrue(all(p[2] >= 200 for p in placed))

    def test_tool_counts_words_and_phrases(self):
        rows = [['Data quality matters. Data quality is key.'], ['The data pipeline needs data quality checks.']]
        out, _ = run_tool('core.word_cloud', {'text_field': 'text', 'generate_phrases': True}, {'input': (F, rows)})
        words = {w['word']: w['count'] for w in json.loads(out['output'].column('Words')[0])}
        self.assertEqual(words['data quality'], 3)
        self.assertNotIn('quality', words)  # the phrase replaces a word that never appears alone
        self.assertNotIn('the', words)  # English stop words removed
        self.assertTrue(out['output'].column('Chart')[0].startswith('<svg'))


class TextClassificationTests(unittest.TestCase):
    def test_trains_evaluates_and_predicts(self):
        T = [('text', 'v_wstring'), ('label', 'v_wstring')]
        train = [[t, k] for t, k in zip(TOPICS, TRUTH)]
        valid = [['laptop battery screen', 'tech'], ['pizza pasta restaurant', 'food'], ['football goal match', 'sport']]
        for algorithm in ('naive_bayes', 'linear_svc'):
            with self.subTest(algorithm=algorithm):
                nodes = [text_input('t', T, train), text_input('v', T, valid), text_input('n', F, [['a great football team'], [None]]),
                         {'id': 'c', 'tool': 'core.text_classification', 'config': {'text_field': 'text', 'label_field': 'label',
                                                                                     'algorithm': algorithm}},
                         {'id': 'p', 'tool': 'core.ml_predict', 'config': {}}]
                edges = [edge('t', 'output', 'c', 'train'), edge('v', 'output', 'c', 'validation'),
                         edge('c', 'model', 'p', 'model'), edge('n', 'output', 'p', 'input')]
                r = run_workflow(workflow(nodes, edges), base_dir=ROOT)
                self.assertTrue(r.ok, r.error)
                ev = {row['Class']: row for row in r.output('c', 'evaluation').to_dicts()}
                self.assertEqual(ev['(accuracy)']['F1'], 1.0)
                self.assertEqual(ev['food']['Support'], 1)
                out = r.output('p').to_dicts()
                self.assertEqual(out[0]['Prediction'], 'sport')
                self.assertAlmostEqual(sum(out[0][f'Probability_{c}'] for c in ('food', 'sport', 'tech')), 1.0)
                self.assertIsNone(out[1]['Prediction'])

    def test_needs_enough_texts_per_label(self):
        T = [('text', 'v_wstring'), ('label', 'v_wstring')]
        r = run_tool_result('core.text_classification', {'text_field': 'text', 'label_field': 'label'},
                            {'train': (T, [['a', 'x'], ['b', 'x'], ['c', 'y']]), 'validation': (T, [['a', 'x']])})
        self.assertIn('at least 3 training texts', r.error['message'])


if __name__ == '__main__':
    unittest.main()
