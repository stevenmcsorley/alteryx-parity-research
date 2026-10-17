"""Every stored tool example (data/tool-examples.json) still reproduces on its kernel."""
import json
import tempfile
import unittest

from flowengine.examples import EXAMPLES_PATH, replay


class ToolExampleTests(unittest.TestCase):
    def test_every_stored_example_replays(self):
        tools = json.loads(EXAMPLES_PATH.read_text(encoding='utf-8'))['tools']
        self.assertGreater(len(tools), 100)
        failures = []
        with tempfile.TemporaryDirectory() as tmp:
            for tool_id, examples in tools.items():
                for i, example in enumerate(examples):
                    problem = replay(tool_id, example, tmp)
                    if problem:
                        failures.append(f'{tool_id}[{i}]: {problem}')
        self.assertEqual(failures, [], 'regenerate with scripts/build_tool_examples.py if a change was intended')


if __name__ == '__main__':
    unittest.main()
