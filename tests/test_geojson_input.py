"""Input Data reads common GeoJSON features into typed attributes and reusable geometry text."""
import json
import tempfile
import unittest
from pathlib import Path

from flowengine.executor import run_workflow
from helpers import workflow


class GeoJsonInputTests(unittest.TestCase):
    def test_feature_collection_runs_through_spatial_info_without_manual_conversion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'places.geojson'
            path.write_text(json.dumps({'type': 'FeatureCollection', 'features': [
                {'type': 'Feature', 'id': 'place-1', 'properties': {'postcode': '00123', 'visitors': 7},
                 'geometry': {'type': 'Point', 'coordinates': [-0.1276, 51.5072]}},
                {'type': 'Feature', 'properties': {'postcode': '00456', 'visitors': 12},
                 'geometry': {'type': 'Point', 'coordinates': [2.3522, 48.8566]}},
            ]}), encoding='utf-8')
            doc = workflow([
                {'id': 'places', 'tool': 'core.input_data', 'config': {'path': 'places.geojson'}},
                {'id': 'info', 'tool': 'core.spatial_info', 'config': {'geometry_field': 'geometry'}},
            ], [{'from': {'node': 'places', 'anchor': 'output'},
                 'to': {'node': 'info', 'anchor': 'input'}}])
            result = run_workflow(doc, base_dir=folder)
            self.assertEqual(result.status, 'succeeded', result.error)
            table = result.outputs['info']['output']
            self.assertEqual(table.column('postcode'), ['00123', '00456'])
            self.assertEqual(table.column('_feature_id'), ['place-1', None])
            self.assertEqual(table.column('visitors'), [7, 12])
            self.assertEqual(table.column('GeometryType'), ['Point', 'Point'])


if __name__ == '__main__':
    unittest.main()
