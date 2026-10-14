"""Independent, bounded record linkage kernel for the Fuzzy Match catalogue entry.

The scoring profile is explicitly Flowbench-defined. It is not an implementation
of Alteryx's undocumented score aggregation or proprietary match-style datasets.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict

from metaphone import doublemetaphone

from ..errors import ConfigError, DataError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType
from ._common import require_fields


KEY_METHODS = ('whole_field_ci', 'alphanumeric', 'digits_only', 'soundex',
               'double_metaphone', 'soundex_digits', 'double_metaphone_digits')
SCORE_METHODS = ('key_only', 'exact', 'levenshtein', 'jaro', 'best_jaro_levenshtein')
GRANULARITIES = ('character', 'character_no_spaces', 'words', 'words_digits')
PREPROCESS = ('none', 'strip_punctuation', 'strip_punctuation_salutations',
              'strip_punctuation_stop_words', 'basic_address')


def _ascii_letters(value: str) -> str:
    folded = unicodedata.normalize('NFKD', value)
    return ''.join(c for c in folded if c.isascii() and c.isalpha()).upper()


def soundex(value: str, maximum: int = 4) -> str:
    """Classic American Soundex, with deterministic ASCII transliteration."""
    text = _ascii_letters(value)
    if not text or maximum < 1:
        return ''
    codes = {'B': '1', 'F': '1', 'P': '1', 'V': '1', 'C': '2', 'G': '2',
             'J': '2', 'K': '2', 'Q': '2', 'S': '2', 'X': '2', 'Z': '2',
             'D': '3', 'T': '3', 'L': '4', 'M': '5', 'N': '5', 'R': '6'}
    first = text[0]
    out = [first]
    last = codes.get(first, '')
    for char in text[1:]:
        code = codes.get(char, '')
        if char in 'HW':  # Classic Soundex does not let H/W separate equal codes.
            continue
        if code and code != last:
            out.append(code)
        last = code
        if len(out) >= maximum:
            break
    return ''.join(out).ljust(min(maximum, 4), '0')


def levenshtein_distance(left, right) -> int:
    """Memory-bounded edit distance for strings or token sequences."""
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def _jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    radius = max(0, max(len(a), len(b)) // 2 - 1)
    am, bm = [False] * len(a), [False] * len(b)
    matches = 0
    for i, ca in enumerate(a):
        for j in range(max(0, i - radius), min(i + radius + 1, len(b))):
            if not bm[j] and ca == b[j]:
                am[i] = bm[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    aa = [a[i] for i in range(len(a)) if am[i]]
    bb = [b[j] for j in range(len(b)) if bm[j]]
    transpositions = sum(x != y for x, y in zip(aa, bb)) / 2
    m = matches
    return (m / len(a) + m / len(b) + (m - transpositions) / m) / 3


def similarity(left: str, right: str, method: str, granularity: str) -> float:
    """Flowbench v1 similarity: normalised 0..1 edit or Jaro score."""
    a = unicodedata.normalize('NFKC', left).casefold()
    b = unicodedata.normalize('NFKC', right).casefold()
    if granularity == 'character_no_spaces':
        a, b = ''.join(a.split()), ''.join(b.split())
    elif granularity in ('words', 'words_digits'):
        at, bt = sorted(a.split()), sorted(b.split())
        if granularity == 'words_digits':
            ad = sorted(x for x in at if any(c.isdigit() for c in x))
            bd = sorted(x for x in bt if any(c.isdigit() for c in x))
            if ad != bd:
                return 0.0
        a, b = at, bt
    if method == 'exact':
        return float(a == b)
    if method == 'key_only':
        return 1.0
    lev = 1.0 if a == b else 1 - levenshtein_distance(a, b) / max(len(a), len(b), 1)
    if method == 'levenshtein':
        return max(0.0, lev)
    if method == 'jaro':
        if not isinstance(a, str) or not isinstance(b, str):
            a, b = ' '.join(a), ' '.join(b)
        return _jaro(a, b)
    if method == 'best_jaro_levenshtein':
        if not isinstance(a, str) or not isinstance(b, str):
            a, b = ' '.join(a), ' '.join(b)
        return max(lev, _jaro(a, b))
    raise ConfigError(f'Unsupported Flowbench match method {method!r}')


def _preprocess(value: str, method: str) -> str:
    value = unicodedata.normalize('NFKC', value).casefold()
    if method == 'none':
        return ' '.join(value.split())
    if method == 'strip_punctuation':
        value = ''.join(' ' if unicodedata.category(c).startswith('P') else c for c in value)
    elif method == 'strip_punctuation_salutations':
        value = re.sub(r'^\s*(?:mr|mrs|ms|miss|dr|prof)\.?\s+', '', value)
        value = ''.join(' ' if unicodedata.category(c).startswith('P') else c for c in value)
    elif method == 'strip_punctuation_stop_words':
        value = ''.join(' ' if unicodedata.category(c).startswith('P') else c for c in value)
        value = ' '.join(w for w in value.split() if w not in {'and', 'of', 'the'})
    elif method == 'basic_address':
        value = ''.join(' ' if unicodedata.category(c).startswith('P') else c for c in value)
        value = re.sub(r'\b(?:apt|apartment|suite|ste|unit)\s+[\w-]+\b', ' ', value)
        replacements = {'street': 'st', 'avenue': 'ave', 'road': 'rd',
                        'boulevard': 'blvd', 'drive': 'dr', 'lane': 'ln'}
        value = ' '.join(replacements.get(w, w) for w in value.split())
    else:
        raise ConfigError(f'Unsupported preprocessing {method!r}')
    return ' '.join(value.split())


def _candidate_keys(value: str | None, field: dict, row_number: int) -> tuple[str, ...]:
    if value is None:
        policy = field['empty_policy']
        if policy == 'match':
            return ('__FLOWBENCH_EMPTY__',)
        if policy in ('ignore', 'no_match'):
            return ()
        value = ''
    value = _preprocess(str(value), field['preprocess'])
    if not value:
        return ('__FLOWBENCH_EMPTY__',) if field['empty_policy'] == 'match' else ()
    words = value.split() if field['per_word'] else [value]
    ignored = {w.casefold() for w in field['ignore_words']}
    keys = set()
    for word in words:
        if not word or word in ignored or (field['ignore_single_letter_words'] and len(word) == 1):
            continue
        method, limit = field['key_method'], field['max_key_length']
        if method == 'whole_field_ci':
            key_values = [word.casefold()]
        elif method == 'alphanumeric':
            key_values = [''.join(c for c in word.casefold() if c.isalnum())]
        elif method == 'digits_only':
            key_values = [''.join(c for c in word if c.isdigit())]
        elif method == 'soundex':
            key_values = [soundex(word, limit)]
        elif method == 'double_metaphone':
            key_values = list(doublemetaphone(_ascii_letters(word)))
        elif method == 'soundex_digits':
            digits = ''.join(c for c in word if c.isdigit())
            key_values = [soundex(word, limit) + ':' + digits[:limit]]
        elif method == 'double_metaphone_digits':
            digits = ''.join(c for c in word if c.isdigit())
            key_values = [k + ':' + digits[:limit] for k in doublemetaphone(_ascii_letters(word))]
        else:
            raise ConfigError(f'Unsupported candidate key method {method!r}')
        for k in key_values:
            if k:
                # The maximum applies to the generated key, including phonetic codes.
                keys.add(k[:limit])
    return tuple(sorted(keys))


class FuzzyMatch(Tool):
    manifest = ToolManifest(
        tool_id='core.fuzzy_match', version='1.0.0', display_name='Fuzzy Match',
        catalog_ids=('join:fuzzy-match',), inputs=(Anchor('input', 'Input'),),
        outputs=(Anchor('output', 'Output'),), dependencies=('Metaphone==0.6 (BSD-3-Clause)',),
        description='Deterministic Flowbench v1 record linkage with indexed candidate keys and explicit score semantics. '
                    'Imported Alteryx styles that depend on vendor datasets are rejected rather than approximated.',
        config_schema={
            'type': 'object', 'additionalProperties': False,
            'required': ['record_id_field', 'match_fields'],
            'properties': {
                'mode': {'enum': ['purge', 'merge'], 'default': 'purge'},
                'record_id_field': {'type': 'string', 'minLength': 1},
                'source_id_field': {'type': ['string', 'null'], 'default': None},
                'match_threshold': {'type': 'number', 'minimum': 0, 'maximum': 100, 'default': 80},
                'match_fields': {'type': 'array', 'minItems': 1, 'items': {'type': 'object',
                    'additionalProperties': False, 'required': ['field'], 'properties': {
                        'field': {'type': 'string', 'minLength': 1},
                        'key_method': {'enum': list(KEY_METHODS), 'default': 'double_metaphone'},
                        'per_word': {'type': 'boolean', 'default': False},
                        'ignore_words': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
                        'ignore_single_letter_words': {'type': 'boolean', 'default': False},
                        'max_key_length': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 100},
                        'preprocess': {'enum': list(PREPROCESS), 'default': 'none'},
                        'match_method': {'enum': list(SCORE_METHODS), 'default': 'jaro'},
                        'granularity': {'enum': list(GRANULARITIES), 'default': 'character'},
                        'threshold': {'type': 'number', 'minimum': 0, 'maximum': 100, 'default': 0},
                        'weight': {'type': 'number', 'exclusiveMinimum': 0, 'default': 1},
                        'empty_policy': {'enum': ['ignore', 'match', 'no_match'], 'default': 'ignore'},
                    }}},
                'output_score': {'type': 'boolean', 'default': True},
                'output_keys': {'type': 'boolean', 'default': False},
                'output_unmatched': {'type': 'boolean', 'default': False},
                'output_groups': {'type': 'boolean', 'default': False},
                'generate_keys_only': {'type': 'boolean', 'default': False},
                'skip_pairs_in_same_match_group': {'type': 'boolean', 'default': False},
                'max_candidate_pairs': {'type': 'integer', 'minimum': 1, 'maximum': 10000000,
                                        'default': 1000000},
                'max_candidate_work': {'type': 'integer', 'minimum': 1, 'maximum': 100000000,
                                       'default': 20000000},
            },
        })

    def check_config(self, cfg):
        names = [f['field'] for f in cfg['match_fields']]
        if len(set(names)) != len(names):
            raise ConfigError('Fuzzy Match fields must be unique')
        if cfg['mode'] == 'merge' and not cfg['source_id_field']:
            raise ConfigError('Merge mode requires a Source ID field')
        if cfg['mode'] == 'merge' and cfg['source_id_field'] == cfg['record_id_field']:
            raise ConfigError('Record ID and Source ID fields must differ')

    def run(self, ctx, cfg, inputs):
        table: Table = inputs['input']
        id_name = cfg['record_id_field']
        fields = cfg['match_fields']
        required = [id_name, *(f['field'] for f in fields)]
        if cfg['mode'] == 'merge':
            required.append(cfg['source_id_field'])
        require_fields(table.schema, required)
        id_index = table.schema.index(id_name)
        source_index = table.schema.index(cfg['source_id_field']) if cfg['mode'] == 'merge' else None
        match_indexes = [table.schema.index(f['field']) for f in fields]
        records = list(table.rows())
        id_keys = []
        source_values = []
        source_keys = []
        for i, row in enumerate(records):
            record_id = row[id_index]
            if record_id is None:
                raise DataError(f'Fuzzy Match record identifier is null at row {i + 1}')
            id_keys.append((type(record_id).__name__, str(record_id)))
            source_values.append(row[source_index] if source_index is not None else None)
            if source_index is not None and source_values[-1] is None:
                raise DataError(f'Fuzzy Match source identifier is null at row {i + 1}')
            source_keys.append((type(source_values[-1]).__name__, str(source_values[-1]))
                               if source_index is not None else None)
        if len(set(id_keys)) != len(id_keys):
            raise DataError(f'Fuzzy Match record identifier field {id_name!r} must be unique')

        prepared = []
        key_values = []
        for row_no, row in enumerate(records):
            if row_no % 4096 == 0:
                ctx.check_cancelled()
            p_row, k_row = [], []
            for spec, idx in zip(fields, match_indexes):
                val = row[idx]
                if val is not None and not isinstance(val, (str, int, float)):
                    raise DataError(f'Fuzzy Match field {spec["field"]!r} must contain text or numeric values')
                text = None if val is None else _preprocess(str(val), spec['preprocess'])
                p_row.append(text)
                k_row.append(_candidate_keys(val, spec, row_no))
            prepared.append(p_row)
            key_values.append(k_row)

        # Candidate generation uses a separate per-field inverted index. Intersecting
        # each field's pair set enforces agreement on every configured blocking key.
        field_pairs = []
        key_bucket_count = 0
        materialized_pairs = 0
        candidate_work = 0
        for field_no, spec in enumerate(fields):
            buckets = defaultdict(list)
            for row_no, values in enumerate(key_values):
                if row_no % 4096 == 0:
                    ctx.check_cancelled()
                for key in values[field_no]:
                    buckets[key].append(row_no)
            pairs = set()
            for key, indexes in buckets.items():
                key_bucket_count += 1
                indexes.sort(key=lambda i: id_keys[i])
                for a_pos, i in enumerate(indexes):
                    for b_pos in range(a_pos + 1, len(indexes)):
                        j = indexes[b_pos]
                        if cfg['mode'] == 'merge' and source_keys[i] == source_keys[j]:
                            continue
                        pairs.add((i, j))
                        candidate_work += 1
                        if candidate_work % 4096 == 0:
                            ctx.check_cancelled()
                        if candidate_work > cfg['max_candidate_work']:
                            raise DataError(f'Fuzzy Match candidate generation work limit '
                                            f'{cfg["max_candidate_work"]} exceeded; refine blocking keys '
                                            'or raise the explicit limit')
                        if len(pairs) > cfg['max_candidate_pairs']:
                            raise DataError(f'Fuzzy Match candidate pair limit {cfg["max_candidate_pairs"]} exceeded; '
                                            'refine blocking keys or raise the explicit limit')
            materialized_pairs += len(pairs)
            if materialized_pairs > cfg['max_candidate_pairs']:
                raise DataError(f'Fuzzy Match indexed pair limit {cfg["max_candidate_pairs"]} exceeded; '
                                'refine blocking keys or raise the explicit limit')
            field_pairs.append(pairs)
        candidates = set.intersection(*field_pairs) if field_pairs else set()
        if len(candidates) > cfg['max_candidate_pairs']:
            raise DataError(f'Fuzzy Match candidate pair limit {cfg["max_candidate_pairs"]} exceeded')
        ordered_candidates = sorted(candidates, key=lambda p: (id_keys[p[0]], id_keys[p[1]]))

        accepted = []
        paired = set()
        cluster_parent = list(range(len(records)))

        def cluster_find(x):
            while cluster_parent[x] != x:
                cluster_parent[x] = cluster_parent[cluster_parent[x]]
                x = cluster_parent[x]
            return x

        compared = 0
        for i, j in ordered_candidates:
            if cfg['skip_pairs_in_same_match_group']:
                if cluster_find(i) == cluster_find(j):
                    continue
            score_parts = []
            qualifies = True
            for fno, (spec, left, right) in enumerate(zip(fields, prepared[i], prepared[j])):
                if (left is None or right is None) and spec['empty_policy'] == 'ignore':
                    continue
                if (left is None or right is None) and spec['empty_policy'] == 'no_match':
                    qualifies = False
                    break
                left = left or ''
                right = right or ''
                score = similarity(left, right, spec['match_method'], spec['granularity']) * 100
                if score < spec['threshold']:
                    qualifies = False
                    break
                score_parts.append((score, spec['weight']))
            compared += 1
            if compared % 1024 == 0:
                ctx.check_cancelled()
            if qualifies and score_parts:
                total_weight = sum(w for _, w in score_parts)
                overall = sum(s * w for s, w in score_parts) / total_weight
                if overall >= cfg['match_threshold']:
                    accepted.append((i, j, overall))
                    paired.update((i, j))
                    left_root, right_root = cluster_find(i), cluster_find(j)
                    if left_root != right_root:
                        cluster_parent[max(left_root, right_root)] = min(left_root, right_root)

        id_field = table.schema.field(id_name)
        if cfg['generate_keys_only']:
            output_fields = [Field('RecordID', id_field.type, id_field.size, id_field.scale)]
        else:
            output_fields = [Field('RecordID1', id_field.type, id_field.size, id_field.scale),
                             Field('RecordID2', id_field.type, id_field.size, id_field.scale)]
        if cfg['output_score'] and not cfg['generate_keys_only']:
            output_fields.append(Field('MatchScore', FieldType.DOUBLE, 8))
        key_column_names = []
        if cfg['output_keys'] or cfg['generate_keys_only']:
            for spec in fields:
                name = 'Key_' + spec['field']
                if name in key_column_names or name in {f.name for f in output_fields}:
                    raise ConfigError(f'Generated key output field collision: {name!r}')
                key_column_names.append(name)
                output_fields.append(Field(name, FieldType.V_WSTRING, 2**30 - 1))
        group_ids = {}
        if cfg['output_groups'] and not cfg['generate_keys_only']:
            roots = sorted({cluster_find(i) for i in paired}, key=lambda i: id_keys[i])
            group_ids = {r: n + 1 for n, r in enumerate(roots)}
            output_fields.append(Field('MatchGroup', FieldType.INT32, 4))

        output = []
        if cfg['generate_keys_only']:
            for i, row in enumerate(records):
                values = [json.dumps(keys, ensure_ascii=False, separators=(',', ':')) for keys in key_values[i]]
                output.append((row[id_index], *values))
        else:
            for i, j, score in accepted:
                root = cluster_find(i) if cfg['output_groups'] else None
                values = [records[i][id_index], records[j][id_index]]
                if cfg['output_score']:
                    values.append(score)
                if cfg['output_keys']:
                    values.extend(json.dumps(key_values[i][fno], ensure_ascii=False, separators=(',', ':'))
                                  for fno in range(len(fields)))
                if cfg['output_groups']:
                    values.append(group_ids[root])
                output.append(tuple(values))
            if cfg['output_unmatched']:
                for i, row in enumerate(records):
                    if i not in paired:
                        values = [row[id_index], None]
                        if cfg['output_score']:
                            values.append(None)
                        if cfg['output_keys']:
                            values.extend(json.dumps(key_values[i][fno], ensure_ascii=False, separators=(',', ':'))
                                          for fno in range(len(fields)))
                        if cfg['output_groups']:
                            values.append(None)
                        output.append(tuple(values))
        id_order = {(type(row[id_index]).__name__, str(row[id_index])): id_keys[i]
                    for i, row in enumerate(records)}
        if cfg['generate_keys_only']:
            output.sort(key=lambda r: id_order[(type(r[0]).__name__, str(r[0]))])
        else:
            output.sort(key=lambda r: (id_order[(type(r[0]).__name__, str(r[0]))],
                                       ('', '') if r[1] is None else id_order[(type(r[1]).__name__, str(r[1]))]))
        ctx.info('Fuzzy Match candidate execution complete', input_records=len(records),
                 candidate_pairs=len(candidates), comparisons=compared, matches=len(accepted),
                 key_buckets=key_bucket_count, mode=cfg['mode'], scoring_profile='flowbench-v1')
        return {'output': Table.from_rows(output_fields, output)}


TOOLS = [FuzzyMatch]
