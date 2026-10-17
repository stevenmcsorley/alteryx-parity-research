"""In-DB tools on real PostgreSQL 16 and SQL Server 2022: results must equal the local engine's.

Start containers with ``python tests/integration/db_env.py up``. Both servers hold the same
``join_cases`` rows (k = 'A', 'a', NULL, NULL, 'É', 'é'), which exercise case, accents and nulls.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from flowengine.db.connections import ConnectionStore
from flowengine.executor import run_workflow

ENV_FILE = Path(__file__).with_name('.db_env.json')
SCHEMA = {'postgresql': 'sales', 'sqlserver': 'ops'}


def edge(a, aa, b, ba):
    return {'from': {'node': a, 'anchor': aa}, 'to': {'node': b, 'anchor': ba}}


def wf(nodes, edges):
    return {'format': 'flowengine.workflow', 'formatVersion': 1, 'nodes': nodes, 'edges': edges}


def rows(table):
    def key(r):
        return tuple((v is None, str(v)) for v in r)
    return sorted((tuple(float(v) if hasattr(v, 'as_tuple') else v for v in r) for r in table.rows()), key=key)


@unittest.skipUnless(ENV_FILE.is_file(), 'real database containers are not provisioned')
class InDBIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env_before = {k: os.environ.get(k) for k in ('FLOWENGINE_HOME', 'FLOWBENCH_VAULT')}
        cls.root = tempfile.TemporaryDirectory(prefix='flowbench-indb-test-')
        os.environ['FLOWENGINE_HOME'] = cls.root.name
        os.environ['FLOWBENCH_VAULT'] = 'env'
        from flowengine.db import vault as vault_module
        vault_module._VAULT = None
        cfg = json.loads(ENV_FILE.read_text(encoding='utf-8'))
        store = ConnectionStore()
        for dialect in ('postgresql', 'sqlserver'):
            server = cfg[dialect]
            for role, writes in (('reader', False), ('writer', True)):
                cid = f'indb_{role}_{dialect}'
                os.environ['FLOWBENCH_SECRET_' + cid.upper()] = server[role]['password']
                store.save({'id': cid, 'name': f'{dialect} {role}', 'dialect': dialect, 'host': server['host'],
                            'port': server['port'], 'database': server['database'], 'user': server[role]['user'],
                            'tls': 'require' if dialect == 'postgresql' else 'disable', 'ca_file': server.get('ca_file'),
                            'allow_writes': writes, 'query_timeout': 30})

    @classmethod
    def tearDownClass(cls):
        for k, value in cls.env_before.items():
            os.environ.pop(k, None) if value is None else os.environ.__setitem__(k, value)
        for k in [k for k in os.environ if k.startswith('FLOWBENCH_SECRET_INDB_')]:
            os.environ.pop(k)
        from flowengine.db import vault as vault_module
        vault_module._VAULT = None
        cls.root.cleanup()

    def run_both(self, build, limits=None):
        for dialect in ('postgresql', 'sqlserver'):
            with self.subTest(dialect=dialect):
                nodes, edges, check = build(dialect, f'indb_reader_{dialect}', f'indb_writer_{dialect}')
                result = run_workflow(wf(nodes, edges), base_dir=Path(self.root.name), limits=limits or {})
                self.assertTrue(result.ok, result.error)
                check(result)

    def connect(self, nid, cid, dialect, table='join_cases'):
        return {'id': nid, 'tool': 'core.connect_indb', 'config': {'connection_id': cid, 'mode': 'table',
                                                                  'schema': SCHEMA[dialect], 'table': table}}

    def test_filter_and_formula_match_the_local_engine(self):
        def build(dialect, cid, _):
            nodes = [self.connect('c', cid, dialect),
                     {'id': 'f', 'tool': 'core.filter_indb', 'config': {'expression': '[k] = "a" OR [amount] > 6'}},
                     {'id': 'fx', 'tool': 'core.formula_indb', 'config': {'formulas': [
                         {'field': 'double', 'expression': '[amount] * 2'}, {'field': 'big', 'expression': '[amount] > 3'}]}},
                     {'id': 'db_true', 'tool': 'core.data_stream_out', 'config': {}},
                     {'id': 'db_false', 'tool': 'core.data_stream_out', 'config': {}},
                     {'id': 'all', 'tool': 'core.data_stream_out', 'config': {}},
                     {'id': 'local', 'tool': 'core.filter', 'config': {'expression': '[k] = "a" OR [amount] > 6'}},
                     {'id': 'lfx', 'tool': 'core.formula', 'config': {'formulas': [
                         {'field': 'double', 'expression': '[amount] * 2', 'type': 'double'},
                         {'field': 'big', 'expression': '[amount] > 3', 'type': 'bool'}]}}]
            edges = [edge('c', 'output', 'f', 'input'), edge('f', 'true', 'fx', 'input'), edge('fx', 'output', 'db_true', 'input'),
                     edge('f', 'false', 'db_false', 'input'), edge('c', 'output', 'all', 'input'),
                     edge('all', 'output', 'local', 'input'), edge('local', 'true', 'lfx', 'input')]

            def check(r):
                self.assertEqual([x['k'] for x in r.output('db_true').to_dicts()].count(None), 0)
                self.assertEqual(rows(r.output('db_true')), rows(r.output('lfx')))  # same records and values
                self.assertEqual(len(r.output('db_true')) + len(r.output('db_false')), 6)
                self.assertEqual(sorted(x['k'] for x in r.output('db_true').to_dicts()), ['A', 'a', 'é'])
            return nodes, edges, check
        self.run_both(build)

    def test_summarize_join_union_and_sample(self):
        def build(dialect, cid, _):
            actions = [{'field': 'k', 'op': 'group_by'}, {'field': 'amount', 'op': 'count', 'output': 'n'},
                       {'field': 'amount', 'op': 'sum', 'output': 'total'}]
            nodes = [self.connect('c', cid, dialect), self.connect('c2', cid, dialect),
                     {'id': 's', 'tool': 'core.summarize_indb', 'config': {'actions': actions}},
                     {'id': 'j', 'tool': 'core.join_indb', 'config': {'keys': [{'left': 'k', 'right': 'k'}]}},
                     {'id': 'u', 'tool': 'core.union_indb', 'config': {}},
                     {'id': 'smp', 'tool': 'core.sample_indb', 'config': {'method': 'first_n', 'n': 2,
                                                                         'order_by': [{'field': 'seq', 'order': 'desc'}]}},
                     {'id': 'b', 'tool': 'core.browse_indb', 'config': {'preview_rows': 3}}]
            for n in ('s', 'j', 'u', 'smp', 'all'):
                nodes.append({'id': f'{n}_out', 'tool': 'core.data_stream_out', 'config': {}})
            nodes += [{'id': 'ls', 'tool': 'core.summarize', 'config': {'actions': [
                          {'field': 'k', 'op': 'group_by'}, {'field': 'amount', 'op': 'count', 'output': 'n'}]}},
                      {'id': 'lj', 'tool': 'core.join', 'config': {'by': 'fields', 'keys': [{'left': 'k', 'right': 'k'}]}}]
            edges = [edge('c', 'output', 's', 'input'), edge('c', 'output', 'j', 'left'), edge('c2', 'output', 'j', 'right'),
                     edge('c', 'output', 'u', 'inputs'), edge('c2', 'output', 'u', 'inputs'), edge('c', 'output', 'smp', 'input'),
                     edge('u', 'output', 'b', 'input'),
                     edge('s', 'output', 's_out', 'input'), edge('j', 'output', 'j_out', 'input'), edge('b', 'output', 'u_out', 'input'),
                     edge('smp', 'output', 'smp_out', 'input'), edge('c', 'output', 'all_out', 'input'),
                     edge('all_out', 'output', 'ls', 'input'), edge('all_out', 'output', 'lj', 'left'), edge('all_out', 'output', 'lj', 'right')]

            def check(r):
                grouped = {x['k']: (x['n'], round(x['total'], 2)) for x in r.output('s_out').to_dicts()}
                self.assertEqual(grouped, {'A': (1, 1.25), 'a': (1, 2.5), None: (2, 7.75), 'É': (1, 5.25), 'é': (1, 6.5)})
                self.assertEqual({x['k']: x['n'] for x in r.output('ls').to_dicts()}, {k: v[0] for k, v in grouped.items()})
                self.assertEqual(len(r.output('j_out')), len(r.output('lj', 'joined')))  # null keys match, case-sensitive
                self.assertEqual(len(r.output('j_out')), 8)
                self.assertEqual(len(r.output('u_out')), 12)
                self.assertEqual(len(r.browse['b']), 3)
                self.assertEqual([x['seq'] for x in r.output('smp_out').to_dicts()], [6, 5])
            return nodes, edges, check
        self.run_both(build)

    def test_transpose_matches_the_local_engine(self):
        def build(dialect, cid, _):
            nodes = [self.connect('c', cid, dialect),
                     {'id': 't', 'tool': 'core.transpose_indb', 'config': {'key_fields': ['seq'], 'data_fields': ['k', 'amount']}},
                     {'id': 'o', 'tool': 'core.data_stream_out', 'config': {}}]
            edges = [edge('c', 'output', 't', 'input'), edge('t', 'output', 'o', 'input')]

            def check(r):
                got = r.output('o').to_dicts()
                self.assertEqual(len(got), 12)
                self.assertEqual({x['Name'] for x in got}, {'k', 'amount'})
                ks = sorted((x['seq'], x['Value']) for x in got if x['Name'] == 'k')
                self.assertEqual([v for _, v in ks], ['A', 'a', None, None, 'É', 'é'])
                amounts = {x['seq']: float(x['Value']) for x in got if x['Name'] == 'amount'}
                self.assertEqual(sorted(amounts.values()), [1.25, 2.5, 3.75, 4.0, 5.25, 6.5])
            return nodes, edges, check
        self.run_both(build)

    def test_stream_in_dynamic_input_dynamic_output(self):
        def build(dialect, cid, _):
            s = SCHEMA[dialect]
            text = {'id': 'local', 'tool': 'core.text_input', 'config': {'fields': [{'name': 'k', 'type': 'v_wstring', 'size': 10},
                                                                                     {'name': 'label', 'type': 'v_wstring', 'size': 20}],
                                                                          'rows': [['a', 'lower a'], ['É', 'upper e acute']]}}
            queries = {'id': 'queries', 'tool': 'core.text_input', 'config': {
                'fields': [{'name': 'q', 'type': 'v_wstring', 'size': 200}],
                'rows': [[f'SELECT seq, k FROM {s}.join_cases WHERE seq < 3'], [f'SELECT seq, k FROM {s}.join_cases WHERE seq > 5']]}}
            nodes = [text, queries, self.connect('c', cid, dialect),
                     {'id': 'in', 'tool': 'core.data_stream_in', 'config': {'connection_id': cid}},
                     {'id': 'j', 'tool': 'core.join_indb', 'config': {'keys': [{'left': 'k', 'right': 'k'}]}},
                     {'id': 'dyn', 'tool': 'core.dynamic_input_indb', 'config': {'connection_id': cid, 'source_field': 'q'}},
                     {'id': 'sql', 'tool': 'core.dynamic_output_indb', 'config': {}},
                     {'id': 'j_out', 'tool': 'core.data_stream_out', 'config': {}},
                     {'id': 'dyn_out', 'tool': 'core.data_stream_out', 'config': {}}]
            edges = [edge('local', 'output', 'in', 'input'), edge('in', 'output', 'j', 'left'), edge('c', 'output', 'j', 'right'),
                     edge('j', 'output', 'j_out', 'input'), edge('queries', 'output', 'dyn', 'input'),
                     edge('dyn', 'output', 'dyn_out', 'input'), edge('j', 'output', 'sql', 'input')]

            def check(r):
                joined = sorted((x['label'], x['seq']) for x in r.output('j_out').to_dicts())
                self.assertEqual(joined, [('lower a', 2), ('upper e acute', 5)])  # exact, accent- and case-sensitive keys
                self.assertEqual(sorted(x['seq'] for x in r.output('dyn_out').to_dicts()), [1, 2, 6])
                self.assertIn('JOIN', r.output('sql').column('Query')[0])
            return nodes, edges, check
        self.run_both(build)

    def test_write_data_in_db_round_trip(self):
        def build(dialect, _, writer):
            target = {'schema': 'scratch', 'table': 'indb_copy'}
            nodes = [self.connect('c', writer, dialect),
                     {'id': 'f', 'tool': 'core.filter_indb', 'config': {'expression': '!IsNull([k])'}},
                     {'id': 'w', 'tool': 'core.write_indb', 'config': {**target, 'mode': 'replace'}}]
            edges = [edge('c', 'output', 'f', 'input'), edge('f', 'true', 'w', 'input')]

            def check(r):
                again = run_workflow(wf([{'id': 'c', 'tool': 'core.connect_indb', 'config': {'connection_id': writer, 'mode': 'table', **target}},
                                         {'id': 'o', 'tool': 'core.data_stream_out', 'config': {}}],
                                        [edge('c', 'output', 'o', 'input')]), base_dir=Path(self.root.name))
                self.assertTrue(again.ok, again.error)
                self.assertEqual(sorted(x['k'] for x in again.output('o').to_dicts()), ['A', 'a', 'É', 'é'])
            return nodes, edges, check
        self.run_both(build, limits={'allow_database_writes': True})


if __name__ == '__main__':
    unittest.main()
