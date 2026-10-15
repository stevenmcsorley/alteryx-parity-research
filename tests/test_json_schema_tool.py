import json
import unittest

import jsonschema
from flowengine.errors import WorkflowValidationError

from helpers import rows, run_tool


class JSONSchemaToolTests(unittest.TestCase):
    def test_generates_valid_schema_and_appends_it_to_each_row(self):
        data = ([('Source', 'v_wstring')], [['one'], ['two']])
        config = {'title': 'Sales record', 'description': 'A row of sales data', 'fields': [
            {'name': 'amount', 'type': 'number', 'minimum': 0, 'required': True},
            {'name': 'region', 'type': 'enum', 'enum_values': ['North', 'South'], 'default_value': 'North'},
            {'name': 'sold_on', 'type': 'date', 'min_date': '2026-01-01'},
        ]}
        out, _ = run_tool('core.json_schema', config, {'input': data})
        self.assertEqual(rows(out['output'])[0][0], 'one')
        self.assertEqual(out['output'].column('JSON Schema')[0], out['output'].column('JSON Schema')[1])
        generated = json.loads(out['output'].column('JSON Schema')[0])
        jsonschema.Draft202012Validator.check_schema(generated)
        self.assertEqual(generated['required'], ['amount'])
        self.assertEqual(generated['properties']['region']['enum'], ['North', 'South'])
        self.assertEqual(generated['properties']['sold_on']['format'], 'date')
        self.assertEqual(generated['properties']['sold_on']['x-flowbench-min-date'], '2026-01-01')

    def test_schema_only_mode_and_invalid_configuration(self):
        out, _ = run_tool('core.json_schema', {'title': 'One field', 'fields': [{'name': 'ok', 'type': 'boolean'}]}, {})
        generated = json.loads(out['output'].column('JSON Schema')[0])
        self.assertEqual(generated['properties']['ok']['type'], 'boolean')
        with self.assertRaises(WorkflowValidationError):
            run_tool('core.json_schema', {'title': 'Bad', 'fields': [
                {'name': 'same', 'type': 'string'}, {'name': 'same', 'type': 'integer'}]}, {})

