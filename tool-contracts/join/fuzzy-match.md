# Fuzzy Match

> Independent Flowbench capability. The scoring and output contract below is **Flowbench v1**, not a claim of Alteryx parity.

## Identity and evidence

- Catalogue ID: `join:fuzzy-match`
- Kernel: `core.fuzzy_match`
- XML plugin: `AlteryxBasePluginsGui.FuzzyMatch.FuzzyMatch`
- Reference build: Designer 2026.2.0.8 (structural configuration observations only)
- Public behavior reference: [Fuzzy Match Tool](https://help.alteryx.com/current/en/designer/tools/join/fuzzy-match-tool.html)
- Match-option reference: [Fuzzy Match Edit Match Options](https://help.alteryx.com/current/en/designer/tools/join/fuzzy-match-tool/fuzzy-match-edit-match-options.html)
- Implementation: **implemented**; independent cases are listed in the JSON contract.
- Reference verification: **not tested**. E1/E2 Fuzzy Match comparisons are unavailable.
- This tool is **not reference verified**; an independent implementation and passing independent tests do not establish Alteryx behavioural equivalence.

## Flowbench v1 configuration

One input and one output. Purge compares records in one stream; Merge requires a Source ID and excludes records with equal typed source IDs. Record IDs must be present, non-null and unique. At least one match field is required.

Candidate keys are indexed per field, and candidate pairs must share a generated key for every configured match field. Supported keys: case-insensitive whole field, alphanumeric, digits, Soundex, Double Metaphone and the Soundex/Double Metaphone plus digits forms. Per-word keys, ignored words, single-letter exclusion and key-length limits are configurable. Soundex is classic American Soundex; Double Metaphone uses the pinned BSD-licensed `Metaphone==0.6` package.

Scoring styles are key-only, exact, normalized Levenshtein, Jaro and best-of-Jaro/Levenshtein, at character, character-without-spaces, sorted-word and words-with-digits granularity. Multiple fields use a weighted mean after each field threshold. Flowbench v1 accepts an overall threshold from 0 to 100 and uses 80 as its initial value; this is consistent with the public documentation's default, but has not been checked against the pinned GUI configuration.

Outputs contain `RecordID1`, `RecordID2`, optional `MatchScore`, optional generated key columns, and optional `MatchGroup`. Keys-only returns each input ID and the generated key arrays. Optional unmatched rows contain a null second ID and score. Output order is deterministic by typed record ID.

Preprocessing currently supports punctuation stripping, a small English salutations list, removal of `and/of/the`, and basic US street/unit normalization. These transformations are independent and are not vendor datasets.

## Interoperability boundary

The importer translates the supported XML structure for record/source IDs, mode, threshold, output flags, field names, key method, per-word and ignored-word settings, key length, empty handling, field threshold and weight. Imported configurations are flagged partial because Flowbench uses its own score normalization and aggregation.

The importer rejects unknown key/scoring modes, custom/vendor preprocessing, Japanese transformations, word-frequency resources, nickname tables and nickname penalties. The unchanged original node XML is retained and emitted during export. A changed Fuzzy Match configuration currently has no native `.yxmd` exporter and export reports that limitation.

The installed sample contains `JaroTFIDF` and licensed `WordStats` values. Those settings are rejected; no proprietary runtime database is copied or imitated. No Designer execution output has been recorded, so no differential parity result is claimed.

## Limits and performance evidence

`max_candidate_pairs` and `max_candidate_work` are hard limits. Exceeding a limit fails the tool with an actionable error; the kernel never silently truncates comparisons. Cancellation is checked during preparation, index creation, candidate generation and scoring.

Local synthetic 100,000-row check (unique digit keys, zero candidates; Windows/Python 3.12, `tracemalloc`): 14.773 s, 106.8 MiB peak traced allocation, 100,000 key buckets, 0 candidate pairs and 0 comparisons. This is one local measurement, not a cross-machine performance guarantee. A 10,000-row zero-candidate regression test is part of the independent suite.

## Known differences and next work

### Evidence limits

The independent tests establish only the named Flowbench v1 cases below; structural inspection of installed XML establishes configuration shape only. There are no E1/E2 runtime comparisons for Fuzzy Match.

- Alteryx Jaro-TFIDF and overall score/weight formula are not reproduced or verified.
- Soundex leading-letter substitutions, vendor-style score ties, Japanese transliteration/normalization, nickname and word-frequency tables are unsupported.
- Exact Alteryx output schema, grouping details, pair ordering and key encoding remain unverified.
- Next: expand supported import modes only after reference-backed XML evidence, then add authorized E1/E2 differential cases.
