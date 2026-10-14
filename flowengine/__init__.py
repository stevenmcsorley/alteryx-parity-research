"""flowengine: independent, typed workflow execution engine.

Original implementation. Not Alteryx software, not derived from it, and not a
claim of compatibility; see docs/COVERAGE.md for verified status per tool.
"""
__version__ = '0.1.0'

import logging as _logging

_logging.getLogger('flowengine').addHandler(_logging.NullHandler())
