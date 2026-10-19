"""flowengine: independent, typed workflow execution engine.

Original implementation. Not Alteryx software, not derived from it, and not a
claim of compatibility; see docs/COVERAGE.md for verified status per tool.
"""
__version__ = '1.0.0rc1'

import logging as _logging

_logging.getLogger('flowengine').addHandler(_logging.NullHandler())
