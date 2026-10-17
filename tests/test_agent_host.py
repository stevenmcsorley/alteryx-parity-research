"""Agent operations on the local environment: files, archives, commands, Python and databases."""
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from flowengine import agent


class HostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / 'work'
        self.dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, permissions=(), service=None, **arguments):
        return agent.call_envelope(name, arguments, agent.Context(self.dir, permissions, service=service))


class FileTests(HostCase):
    def test_writes_need_permission_stay_in_the_workspace_and_do_not_clobber(self):
        self.assertEqual(self.call('write_text_file', path='notes/a.md', text='x')['error']['code'], 'permission-required')
        ok = self.call('write_text_file', ('file_writes',), path='notes/a.md', text='# Findings\n')
        self.assertTrue(ok['ok'])
        self.assertEqual((self.dir / 'notes' / 'a.md').read_text(), '# Findings\n')
        self.assertEqual(self.call('write_text_file', ('file_writes',), path='notes/a.md', text='y')['error']['code'], 'exists')
        self.assertTrue(self.call('write_text_file', ('file_writes',), path='notes/a.md', text='y', overwrite=True)['ok'])
        outside = self.call('write_text_file', ('file_writes',), path='../escape.txt', text='x')
        self.assertEqual(outside['error']['code'], 'outside-workspace')
        self.assertFalse((self.dir.parent / 'escape.txt').exists())
        read = self.call('read_text_file', path='notes/a.md')['result']
        self.assertEqual((read['text'], read['total_lines']), ('y', 1))

    def test_archives_extract_inside_the_workspace_only(self):
        good = self.dir / 'data.zip'
        with zipfile.ZipFile(good, 'w') as z:
            z.writestr('tables/a.csv', 'k,v\n1,2\n')
        out = self.call('extract_archive', ('file_writes',), path='data.zip')['result']
        self.assertEqual(Path(out['files'][0]).read_text(), 'k,v\n1,2\n')
        bad = self.dir / 'bad.zip'
        with zipfile.ZipFile(bad, 'w') as z:
            z.writestr('../../evil.txt', 'x')
        self.assertEqual(self.call('extract_archive', ('file_writes',), path='bad.zip')['error']['code'], 'unsafe-archive')
        self.assertFalse((Path(self.tmp.name) / 'evil.txt').exists())


class ProcessTests(HostCase):
    def test_commands_need_permission_and_report_output_and_files(self):
        argv = [sys.executable, '-c', "open('out.txt','w').write('hi'); print('done')"]
        self.assertEqual(self.call('run_command', argv=argv)['error']['permissions'], ['processes'])
        out = self.call('run_command', ('processes',), argv=argv)['result']
        self.assertEqual((out['exit_code'], out['stdout'].strip()), (0, 'done'))
        self.assertEqual(out['files_written'], [str(self.dir / 'out.txt')])
        self.assertEqual(self.call('run_command', ('processes',), argv=['no-such-program-xyz'])['error']['code'], 'not-found')

    def test_python_scripts_are_recorded_and_their_files_feed_flowbench(self):
        code = "import pandas as pd\npd.DataFrame({'region': ['N', 'S'], 'sales': [3, 4]}).to_csv('prepared.csv', index=False)\nprint('rows', 2)"
        out = self.call('run_python', ('processes',), code=code)['result']
        self.assertEqual(out['exit_code'], 0, out['stderr'])
        self.assertTrue(Path(out['script']).read_text().startswith('import pandas'))
        self.assertIn(str(self.dir / 'prepared.csv'), out['files_written'])
        self.assertEqual(self.call('profile_file', path='prepared.csv')['result']['rows'], 2)


class DatabaseTests(HostCase):
    def test_database_operations_use_the_engine_connection_service(self):
        calls = []

        class Service:
            def handle(self, method, params):
                calls.append((method, params))
                return {'database.connections.list': [{'id': 'c1', 'name': 'Warehouse', 'dialect': 'postgresql'}],
                        'database.schemas': ['public'], 'database.tables': ['sales'],
                        'database.columns': [{'name': 'amount', 'type': 'double'}],
                        'database.preview': {'fields': [], 'total_rows': 0, 'rows': []}}[method]
        svc = Service()
        self.assertEqual(self.call('list_connections', service=svc)['result']['connections'][0]['name'], 'Warehouse')
        self.assertEqual(self.call('list_tables', service=svc, connection_id='c1')['result'], {'schemas': ['public']})
        self.assertEqual(self.call('list_tables', service=svc, connection_id='c1', schema='public')['result']['tables'], ['sales'])
        self.assertEqual(self.call('describe_table', service=svc, connection_id='c1', table='sales')['result']['columns'][0]['name'], 'amount')
        self.call('query_database', service=svc, connection_id='c1', sql='select 1')
        self.assertEqual(calls[-1], ('database.preview', {'connection_id': 'c1', 'mode': 'query', 'query': 'select 1', 'limit': 100}))
        self.assertEqual(self.call('list_connections')['error']['code'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
