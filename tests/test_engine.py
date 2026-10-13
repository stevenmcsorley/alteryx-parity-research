"""Independent engine semantic tests: these do not prove Alteryx parity."""
import json
import tempfile
import unittest
from pathlib import Path
from engine.independent_engine.runtime import Table, WorkflowError, run_workflow, run_from_file

ROOT=Path(__file__).resolve().parents[1]

class TestOriginalEngine(unittest.TestCase):
    def test_real_csv_pipeline(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t); doc=json.loads((ROOT/'examples/customer_reconciliation.json').read_text())
            for row in doc['nodes']:
                if row['type']=='csv_input':row['config']['path']=str((ROOT/'examples'/row['config']['path']).resolve())
            out=run_workflow(doc,base=path)
            self.assertEqual(out['totals'].columns,('customer_id','name','total_spend'))
            self.assertEqual(list(out['totals'].rows),[{'customer_id':'101','name':'Ada','total_spend':'20.00'}])
            self.assertTrue((path/'out/uk-customer-spend.csv').exists())

    def test_duplicate_key_join_multiplies_rows(self):
        a={'nodes':[
          {'id':'c','type':'csv_input','config':{'path':str((ROOT/'examples/data/customers.csv').resolve())}},
          {'id':'o','type':'csv_input','config':{'path':str((ROOT/'examples/data/orders.csv').resolve())}},
          {'id':'j','type':'join','config':{'left_on':'customer_id','right_on':'customer_id'}}],
          'edges':[{'from':'c','to':'j','input':'left'},{'from':'o','to':'j','input':'right'}]}
        result=run_workflow(a,base=ROOT)
        self.assertEqual(len(result['j'].rows),4)

    def test_cycle_detection(self):
        doc={'nodes':[{'id':'a','type':'filter','config':{'field':'x','value':'y'}},{'id':'b','type':'filter','config':{'field':'x','value':'y'}}],
             'edges':[{'from':'a','to':'b'},{'from':'b','to':'a'}]}
        with self.assertRaisesRegex(WorkflowError,'Cycle detected'):run_workflow(doc,base=ROOT)

    def test_unknown_operator_fails(self):
        with self.assertRaisesRegex(WorkflowError,'Unimplemented operator'):
            run_workflow({'nodes':[{'id':'x','type':'fictional','config':{}}],'edges':[]},base=ROOT)

    def test_table_shape_check(self):
        with self.assertRaisesRegex(WorkflowError,'shape'):
            Table(('x',),({'y':'wrong'},))

    def test_empty_pipeline(self):
        self.assertEqual(run_workflow({'nodes':[],'edges':[]},base=ROOT),{})

if __name__=='__main__':unittest.main()
