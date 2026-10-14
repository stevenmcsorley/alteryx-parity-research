# Data Cleanse Pro

## Current Flowbench implementation

- Catalogue ID: `preparation:data-cleanse-pro`
- Independent kernel: `core.data_cleanse_pro` in `flowengine/tools/preparation.py`
- Anchors: `input` → `output`
- Desktop configuration: schema-driven inspector backed by the kernel manifest
- Independent tests: Unicode and HTML cleanup, invisible format characters, character-category exceptions, null replacement/removal, decimal preservation, selected columns and missing-field errors (`tests/test_fe_tools.py`)
- Generated status: see `data/tool-status.json` and `docs/ALTERYX_PARITY_ROADMAP.md`

The implementation covers the documented cleansing options: selected fields,
leading/trailing and repeated whitespace, all-whitespace removal, markup and
format-character removal, Unicode letters/numbers/punctuation and symbols,
exceptions, string and numeric null/blank replacement, case conversion, and
post-transform null row/column removal. Value-dependent column removal stops
static schema inference rather than claiming a fixed output schema. It disables
streaming where the selected null removal requires a whole-table decision.

## Evidence limits

This implementation is independently tested, not reference verified. The
public documentation describes AMP-only behavior; current Alteryx 2026.2.0.8
GUI execution has not been compared. Python Unicode categories are used for
letter/number/punctuation/symbol classification; exact equivalence to the
reference ICU version and its exceptional code points is unknown. Exact default
and interaction behavior of all target XML options is also unverified. The
reference configuration element names remain recorded in the machine-readable
contract. XML configuration import is not currently translated; unsupported
original settings must remain explicit in import diagnostics.

The generated ledger's unreviewed `vendor-cloud-service` dependency signal is
from page keyword scanning and is not required by this local kernel.

## Public source

Official documentation: <https://help.alteryx.com/current/en/designer/tools/preparation/data-cleanse-pro-tool.html>
(checked 2026-10-14). It describes the selected-field behavior, null row/column
removal after other cleanup, whitespace and markup handling, Unicode character
categories, null/blank replacements, and case modes. Public documentation is
not used as runtime parity evidence.
