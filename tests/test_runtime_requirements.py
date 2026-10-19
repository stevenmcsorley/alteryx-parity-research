"""The installers bundle the pinned runtime requirements, not requirements.txt; keep the two in step."""
import re
import unittest
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def _requirements(name):
    out = []
    for line in (ROOT / name).read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if line and not line.startswith('-r'):
            out.append(Requirement(line))
    return out


def _key(name):
    return re.sub(r'[-_.]+', '-', name).lower()


class RuntimeRequirementsTests(unittest.TestCase):
    def test_every_engine_dependency_is_pinned_in_the_bundled_runtime(self):
        pins = {_key(r.name): r for r in _requirements('requirements-windows-runtime.txt')}
        for req in _requirements('requirements.txt'):
            names = [req.name] + [f'{req.name}-{extra}' for extra in req.extras]
            for name in names:
                with self.subTest(requirement=name):
                    self.assertIn(_key(name), pins, f'{name} is in requirements.txt but not bundled in the installers')
            pin = pins[_key(req.name)]
            version = next(iter(pin.specifier)).version
            with self.subTest(requirement=str(req)):
                self.assertTrue(req.specifier.contains(version, prereleases=True),
                                f'bundled {pin} does not satisfy requirements.txt {req}')

    def test_runtime_pins_are_exact(self):
        for req in _requirements('requirements-windows-runtime.txt') + _requirements('requirements-unix-runtime.txt'):
            with self.subTest(requirement=str(req)):
                self.assertEqual([s.operator for s in req.specifier], ['=='])


if __name__ == '__main__':
    unittest.main()
