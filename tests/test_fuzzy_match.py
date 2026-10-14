"""Independent tests for the explicitly Flowbench-defined Fuzzy Match profile."""
import time
import unittest

from flowengine.errors import WorkflowValidationError
from flowengine.plugin import CancelToken
from flowengine.tools.fuzzy_match import levenshtein_distance, similarity, soundex
from helpers import rows, run_tool, run_tool_result


def field(name, **options):
    return {'field': name, **options}


def config(**overrides):
    value = {'mode': 'purge', 'record_id_field': 'id', 'match_threshold': 70,
             'match_fields': [field('name', key_method='double_metaphone', match_method='jaro')],
             'output_score': True, 'output_keys': False, 'output_unmatched': False,
             'output_groups': False, 'generate_keys_only': False,
             'skip_pairs_in_same_match_group': False, 'max_candidate_pairs': 100000,
             'max_candidate_work': 20000000}
    value.update(overrides)
    return value


class FuzzyAlgorithmTests(unittest.TestCase):
    def test_known_levenshtein_distances_and_normalised_scores(self):
        self.assertEqual(levenshtein_distance('kitten', 'sitting'), 3)
        self.assertAlmostEqual(similarity('kitten', 'sitting', 'levenshtein', 'character'), 4 / 7)
        self.assertEqual(similarity('A B', 'ab', 'exact', 'character_no_spaces'), 1)
        self.assertAlmostEqual(similarity('MARTHA', 'MARHTA', 'jaro', 'character'), 17 / 18)
        self.assertGreaterEqual(similarity('MARTHA', 'MARHTA', 'best_jaro_levenshtein', 'character'),
                                similarity('MARTHA', 'MARHTA', 'jaro', 'character'))

    def test_word_modes_and_digit_guard(self):
        self.assertEqual(similarity('smith jones', 'jones smith', 'exact', 'words'), 1)
        self.assertEqual(similarity('12 Main Street', '13 Main Street', 'levenshtein', 'words_digits'), 0)
        self.assertEqual(similarity('12 Main Street', '12 Main Street', 'levenshtein', 'words_digits'), 1)
        self.assertEqual(similarity('different spellings', 'different spellings', 'key_only', 'character'), 1)

    def test_classic_soundex_and_double_metaphone_candidate_collisions(self):
        self.assertEqual(soundex('Robert'), 'R163')
        self.assertEqual(soundex('Rupert'), 'R163')
        self.assertEqual(soundex('Ashcraft'), 'A261')
        from flowengine.tools.fuzzy_match import _candidate_keys
        spec = {'empty_policy': 'ignore', 'preprocess': 'none', 'per_word': False,
                'ignore_words': [], 'ignore_single_letter_words': False,
                'max_key_length': 100, 'key_method': 'double_metaphone'}
        self.assertTrue(set(_candidate_keys('Smith', spec, 0)) & set(_candidate_keys('Schmidt', spec, 1)))


class FuzzyMatchTests(unittest.TestCase):
    def test_purge_emits_candidate_pairs_scores_and_stable_order(self):
        data = ([('id', 'int32'), ('name', 'v_string')],
                [[3, 'Smith'], [1, 'Schmidt'], [2, 'Jones']])
        out, _ = run_tool('core.fuzzy_match', config(match_threshold=60), {'input': data})
        self.assertEqual(out['output'].schema.names, ['RecordID1', 'RecordID2', 'MatchScore'])
        pairs = rows(out['output'])
        self.assertEqual([(r[0], r[1]) for r in pairs], [(1, 3)])
        self.assertGreaterEqual(pairs[0][2], 60)

    def test_merge_excludes_same_sources_and_requires_source_field(self):
        data = ([('id', 'int32'), ('src', 'v_string'), ('name', 'v_string')],
                [[1, 'A', 'Robert'], [2, 'A', 'Rupert'], [3, 'B', 'Rupert']])
        cfg = config(mode='merge', source_id_field='src', match_threshold=60,
                     match_fields=[field('name', key_method='soundex', match_method='levenshtein')])
        out, _ = run_tool('core.fuzzy_match', cfg, {'input': data})
        self.assertEqual([(r[0], r[1]) for r in rows(out['output'])], [(1, 3), (2, 3)])
        with self.assertRaisesRegex(WorkflowValidationError, 'Source ID'):
            run_tool_result('core.fuzzy_match', config(mode='merge'), {'input': data})

    def test_purge_ignores_source_id_even_if_imported_xml_reuses_record_id(self):
        data = ([('id', 'int32'), ('name', 'v_string')], [[1, 'Robert'], [2, 'Rupert']])
        cfg = config(source_id_field='id', match_threshold=40,
                     match_fields=[field('name', key_method='soundex', match_method='levenshtein')])
        out, _ = run_tool('core.fuzzy_match', cfg, {'input': data})
        self.assertEqual([(r[0], r[1]) for r in rows(out['output'])], [(1, 2)])

    def test_key_only_scoring_accepts_key_collisions_without_string_score(self):
        data = ([('id', 'int32'), ('name', 'v_string')], [[1, 'Robert'], [2, 'Rupert']])
        cfg = config(match_threshold=100,
                     match_fields=[field('name', key_method='soundex', match_method='key_only')])
        out, _ = run_tool('core.fuzzy_match', cfg, {'input': data})
        result = rows(out['output'])
        self.assertEqual([(r[0], r[1]) for r in result], [(1, 2)])
        self.assertEqual(result[0][2], 100)

    def test_multiple_weighted_fields_threshold_and_punctuation_preprocessing(self):
        data = ([('id', 'int32'), ('company', 'v_string'), ('zip', 'v_string')],
                [[1, 'Acme, Inc.', '10001'], [2, 'ACME Inc', '10001'], [3, 'Acme Inc', '10002']])
        cfg = config(record_id_field='id', match_threshold=80, match_fields=[
            field('company', key_method='alphanumeric', preprocess='strip_punctuation',
                  match_method='exact', threshold=100, weight=3),
            field('zip', key_method='digits_only', match_method='exact', threshold=100, weight=1)])
        out, _ = run_tool('core.fuzzy_match', cfg, {'input': data})
        self.assertEqual([(r[0], r[1]) for r in rows(out['output'])], [(1, 2)])

    def test_threshold_boundary_is_inclusive(self):
        data = ([('id', 'int32'), ('name', 'v_string')], [[1, 'kitten'], [2, 'sitting']])
        cfg = config(match_threshold=100 * 6 / 7,
                     match_fields=[field('name', key_method='digits_only', match_method='levenshtein')])
        data = ([('id', 'int32'), ('name', 'v_string')], [[1, 'kitten1'], [2, 'sitten1']])
        out, _ = run_tool('core.fuzzy_match', cfg, {'input': data})
        self.assertEqual(len(out['output']), 1)
        cfg['match_threshold'] += 0.00001
        out, _ = run_tool('core.fuzzy_match', cfg, {'input': data})
        self.assertEqual(len(out['output']), 0)

    def test_unmatched_null_empty_and_keys_only(self):
        data = ([('id', 'int32'), ('name', 'v_string')], [[1, 'Smith'], [2, None], [3, '']])
        cfg = config(output_unmatched=True, output_keys=True)
        out, _ = run_tool('core.fuzzy_match', cfg, {'input': data})
        self.assertEqual(out['output'].schema.names, ['RecordID1', 'RecordID2', 'MatchScore', 'Key_name'])
        self.assertEqual([(r[0], r[1]) for r in rows(out['output'])], [(1, None), (2, None), (3, None)])
        keys_cfg = config(generate_keys_only=True, output_keys=False)
        keys, _ = run_tool('core.fuzzy_match', keys_cfg, {'input': data})
        self.assertEqual(keys['output'].schema.names, ['RecordID', 'Key_name'])
        self.assertEqual(len(keys['output']), 3)

    def test_empty_values_can_match_only_when_explicit(self):
        data = ([('id', 'int32'), ('name', 'v_string')], [[1, None], [2, '']])
        cfg = config(match_fields=[field('name', key_method='alphanumeric', match_method='exact', empty_policy='match')])
        out, _ = run_tool('core.fuzzy_match', cfg, {'input': data})
        self.assertEqual([(r[0], r[1]) for r in rows(out['output'])], [(1, 2)])

    def test_duplicate_and_null_identifiers_fail(self):
        cfg = config()
        for data in [([('id', 'int32'), ('name', 'v_string')], [[1, 'A'], [1, 'B']]),
                     ([('id', 'int32'), ('name', 'v_string')], [[None, 'A']])]:
            result = run_tool_result('core.fuzzy_match', cfg, {'input': data})
            self.assertFalse(result.ok)
            self.assertTrue('identifier' in result.error['message'])

    def test_invalid_settings_and_field_names_fail_clearly(self):
        data = ([('id', 'int32'), ('name', 'v_string')], [[1, 'a']])
        for invalid in [config(match_threshold=101), config(match_fields=[]),
                        config(match_fields=[field('name', key_method='not-supported')]),
                        config(match_fields=[field('missing')])]:
            with self.assertRaises(WorkflowValidationError):
                run_tool_result('core.fuzzy_match', invalid, {'input': data})

    def test_unicode_exact_supported_phonetic_boundary_is_explicit(self):
        data = ([('id', 'int32'), ('name', 'v_string')], [[1, '東京'], [2, '東京']])
        cfg = config(match_fields=[field('name', key_method='whole_field_ci', match_method='exact')])
        out, _ = run_tool('core.fuzzy_match', cfg, {'input': data})
        self.assertEqual([(r[0], r[1]) for r in rows(out['output'])], [(1, 2)])

    def test_preprocessing_variants_and_key_length_are_bounded(self):
        from flowengine.tools.fuzzy_match import _candidate_keys, _preprocess
        self.assertEqual(_preprocess('Dr.  SMITH, Jr.', 'strip_punctuation_salutations'), 'smith jr')
        self.assertEqual(_preprocess('The Acme, Inc.', 'strip_punctuation_stop_words'), 'acme inc')
        self.assertEqual(_preprocess('12 Main Street, Apt 4', 'basic_address'), '12 main st')
        spec = {'empty_policy': 'ignore', 'preprocess': 'none', 'per_word': False,
                'ignore_words': [], 'ignore_single_letter_words': False,
                'max_key_length': 2, 'key_method': 'double_metaphone'}
        self.assertTrue(all(len(k) <= 2 for k in _candidate_keys('Schmidt', spec, 0)))

    def test_candidate_work_limit_fails_without_truncating(self):
        data = ([('id', 'int32'), ('name', 'v_string')], [[1, 'Robert'], [2, 'Rupert'], [3, 'Rubert']])
        cfg = config(match_fields=[field('name', key_method='soundex')], max_candidate_pairs=1)
        result = run_tool_result('core.fuzzy_match', cfg, {'input': data})
        self.assertFalse(result.ok)
        self.assertIn('candidate pair limit', result.error['message'])

    def test_repeated_block_keys_have_an_explicit_generation_work_limit(self):
        shared = ' '.join(f'word{i}' for i in range(30))
        data = ([('id', 'int32'), ('name', 'v_string')], [[1, shared], [2, shared]])
        cfg = config(match_fields=[field('name', per_word=True, key_method='whole_field_ci')],
                     max_candidate_pairs=10, max_candidate_work=5)
        result = run_tool_result('core.fuzzy_match', cfg, {'input': data})
        self.assertFalse(result.ok)
        self.assertIn('candidate generation work limit', result.error['message'])

    def test_cancellation_is_reported_without_partial_results(self):
        token = CancelToken()
        token.cancel()
        data = ([('id', 'int32'), ('name', 'v_string')], [[1, 'Smith'], [2, 'Smith']])
        result = run_tool_result('core.fuzzy_match', config(), {'input': data}, cancel_token=token)
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(result.outputs, {})

    def test_10k_records_use_indexed_candidates(self):
        n = 10000
        data = ([('id', 'int32'), ('key', 'v_string')], [[i, str(i)] for i in range(n)])
        cfg = config(match_fields=[field('key', key_method='digits_only', match_method='exact')],
                     generate_keys_only=False, max_candidate_pairs=1000)
        started = time.perf_counter()
        out, result = run_tool('core.fuzzy_match', cfg, {'input': data})
        self.assertEqual(len(out['output']), 0)
        self.assertLess(time.perf_counter() - started, 10)
        event = next(e for e in result.events if e.get('message') == 'Fuzzy Match candidate execution complete')
        self.assertEqual(event.get('candidate_pairs'), 0)


if __name__ == '__main__':
    unittest.main()
