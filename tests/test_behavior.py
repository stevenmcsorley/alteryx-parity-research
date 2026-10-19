"""Behavior Analysis tools with a synthetic segmentation data pack (no licensed data)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flowengine import providers
from flowengine.executor import run_workflow

from helpers import edge, text_input, workflow


class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.dir = Path(self._home.name)
        self._env = mock.patch.dict(os.environ, {'FLOWENGINE_HOME': self._home.name})
        self._env.start()
        (self.dir / 'seg.csv').write_text('GeoKey,Cluster,Count,ClusterName,Group\n'
                                          'G1,A,60,Urban families,Urban\nG1,B,40,Retirees,Rural\n'
                                          'G2,A,10,Urban families,Urban\nG2,B,90,Retirees,Rural\nG3,C,5,Students,Urban\n')
        providers.DataPackStore().register('segmentation', str(self.dir / 'seg.csv'), 'Synthetic (test)')

    def tearDown(self):
        self._env.stop()
        self._home.cleanup()

    def run_tool(self, tool, config, data=None, nodes=None):
        nodes = nodes or ([text_input('t', *data)] if data else [])
        nodes = nodes + [{'id': 'x', 'tool': tool, 'config': {'provider': 'segmentation-data-pack', **config} if tool not in (
            'core.profile_output', 'core.profile_input') else config}]
        edges = [edge(nodes[-2]['id'], 'output', 'x', 'input')] if len(nodes) > 1 else []
        r = run_workflow(workflow(nodes, edges), base_dir=self.dir, limits={})
        self.assertEqual(r.status, 'succeeded', r.error)
        out = r.outputs['x'].get('output')
        return [dict(zip(out.schema.names, row)) for row in out.rows()] if out is not None else None

    def profile(self, mode, field, rows, **extra):
        return self.run_tool('core.create_profile', {'mode': mode, 'field': field, **extra}, ([(field, 'v_wstring'), ('n', 'int32')], rows))

    def test_profiles_from_codes_geographies_and_combinations(self):
        codes = self.profile('cluster_code', 'c', [['A', '3'], ['B', '1'], ['A', '1']], count_field='n', multiplier=2)
        self.assertEqual(json.loads(codes[0]['Profile'])['clusters'], {'A': 8, 'B': 2})
        geo = self.profile('geography_key', 'g', [['G1', '0'], ['G2', '0'], ['G9', '0']], profile_name='Stores')
        self.assertEqual(json.loads(geo[0]['Profile']), {'format': 'flowbench.profile.v1', 'name': 'Stores', 'clusters': {'A': 70, 'B': 130}})
        per = self.profile('geography_key', 'g', [['G1', '0'], ['G3', '0']], summarize='none')
        self.assertEqual([json.loads(p['Profile'])['clusters'] for p in per], [{'A': 60, 'B': 40}, {'C': 5}])

    def test_compare_index_penetration_and_reports(self):
        target = '{"format":"flowbench.profile.v1","name":"Customers","clusters":{"A":30,"B":10}}'
        base = '{"format":"flowbench.profile.v1","name":"Market","clusters":{"A":70,"B":130}}'
        data = ([('Profile', 'v_wstring'), ('BaseProfile', 'v_wstring')], [[target, base]])
        rows = self.run_tool('core.compare_behavior', {}, data)
        a = next(r for r in rows if r['Cluster'] == 'A')
        # target share 30/40 = 0.75; base share 70/200 = 0.35; index 100 * 0.75 / 0.35 = 214.29; penetration 30/70
        self.assertEqual((a['ClusterName'], a['TargetShare'], a['BaseShare'], a['Index']), ('Urban families', 0.75, 0.35, 214.29))
        self.assertAlmostEqual(a['Penetration'], 30 / 70, places=6)
        rank = json.loads(self.run_tool('core.profile_rank_report', {'top': 1}, data)[0]['Report'])
        self.assertEqual((rank['rows'][0][0], rank['rows'][0][-1], len(rank['rows'])), ('A', '214', 1))
        comp = json.loads(self.run_tool('core.profile_comparison_report', {}, data)[0]['Report'])
        self.assertEqual(comp['title'], 'Profile comparison: Customers vs Market')
        detail = json.loads(self.run_tool('core.profile_detail_report', {}, ([('Profile', 'v_wstring')], [[base]]))[0]['Report'])
        self.assertEqual(detail['rows'], [['A', 'Urban families', '70', '35.0%'], ['B', 'Retirees', '130', '65.0%']])

    def test_profile_sets_detail_fields_cluster_codes_and_metainfo(self):
        p = '{"format":"flowbench.profile.v1","name":"X","clusters":{"A":1,"B":3}}'
        self.run_tool('core.profile_output', {'path': 'sets/mine.fbprof'}, ([('Profile', 'v_wstring'), ('ProfileName', 'v_wstring')], [[p, 'Mine']]))
        back = self.run_tool('core.profile_input', {'path': 'sets/mine.fbprof'})
        self.assertEqual((back[0]['ProfileName'], json.loads(back[0]['Profile'])['clusters']), ('Mine', {'A': 1, 'B': 3}))
        det = self.run_tool('core.detail_fields', {}, ([('Profile', 'v_wstring')], [[p]]))[0]
        self.assertEqual((det['Cluster_A'], det['Cluster_B_Pct'], det['Cluster_C']), (1, 75.0, 0))
        codes = self.run_tool('core.cluster_code', {'key_field': 'g'}, ([('g', 'v_wstring')], [['G1'], ['G2'], ['G4']]))
        self.assertEqual([(c['ClusterCode'], c['ClusterName'], c['ClusterShare']) for c in codes],
                         [('A', 'Urban families', 0.6), ('B', 'Retirees', 0.9), (None, None, None)])
        meta = self.run_tool('core.behavior_metainfo', {})
        self.assertEqual([(m['Cluster'], m['Group'], m['Count']) for m in meta], [('A', 'Urban', 70), ('B', 'Rural', 130), ('C', 'Urban', 5)])


if __name__ == '__main__':
    unittest.main()
