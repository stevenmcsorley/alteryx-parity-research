"""Import/export of .yxmd / .yxmc / .yxwz / .yxzp workflow documents.

Independent implementation of the XML document structure as observed structurally
(docs/AUTHORISATION.md, data/reference-observations.json). Design rules:

* Nothing is dropped. Every node is imported; tools without a converter keep their original
  identifier as ``tool`` (so validation reports ``tool-unavailable`` naming the catalogue entry)
  and their original XML is preserved under ``metadata.import.nodes``.
* Converters translate only settings they understand; anything else is listed as a warning
  in the import report (``partial``), never silently ignored.
* Imported workflows must pass normal validation before they can run.
* Export re-emits the original XML for imported nodes whose configuration is unchanged and
  generates XML for supported native nodes; it refuses (lists) nodes it cannot express.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

REF_TO_TYPE = {'Bool': 'bool', 'Byte': 'byte', 'Int16': 'int16', 'Int32': 'int32', 'Int64': 'int64',
               'FixedDecimal': 'fixed_decimal', 'Float': 'float', 'Double': 'double', 'String': 'string',
               'WString': 'wstring', 'V_String': 'v_string', 'V_WString': 'v_wstring', 'Date': 'date',
               'Time': 'time', 'DateTime': 'datetime', 'Blob': 'blob'}
TYPE_TO_REF = {v: k for k, v in REF_TO_TYPE.items()}
SUMMARIZE_OPS = {'GroupBy': 'group_by', 'Sum': 'sum', 'Count': 'count', 'CountNonNull': 'count_non_null',
                 'CountNull': 'count_null', 'CountDistinct': 'count_distinct', 'CountDistinctNonNull': 'count_distinct_non_null',
                 'Avg': 'avg', 'Min': 'min', 'Max': 'max', 'First': 'first', 'Last': 'last', 'Concat': 'concat',
                 'Median': 'median', 'Mode': 'mode', 'StdDev': 'std_dev', 'Variance': 'variance', 'Longest': 'longest',
                 'Shortest': 'shortest', 'CountBlank': 'count_blank', 'CountNonBlank': 'count_non_blank'}


class Unsupported(Exception):
    """The node (or one of its required settings) has no faithful translation."""


@dataclass
class Converted:
    tool: str
    config: dict
    inputs: dict = field(default_factory=dict)   # original connection name -> our anchor
    outputs: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


# --------------------------------------------------------------------------- helpers

def _t(el, path, default=None):
    x = el.find(path)
    return default if x is None or x.text is None else x.text


def _v(el, path, default=None):
    x = el.find(path)
    return default if x is None else x.get('value', default)


def _b(text) -> bool:
    return str(text).strip().lower() == 'true'


def _type(ref_type: str | None, where: str) -> str:
    if ref_type not in REF_TO_TYPE:
        raise Unsupported(f'{where}: data type {ref_type!r} is not supported')
    return REF_TO_TYPE[ref_type]


def _size(spec: dict, ftype: str, size: str | None):
    if size in (None, ''):
        return spec
    if ftype == 'fixed_decimal' and '.' in size:
        p, s = size.split('.', 1)
        spec['size'], spec['scale'] = int(p), int(s)
    elif ftype in ('string', 'wstring', 'v_string', 'v_wstring'):
        spec['size'] = int(float(size))
    return spec


def _fields(el, path, attr='field') -> list[str]:
    return [f.get(attr) or f.get('name') or f.get('field') for f in el.findall(path)
            if (f.get(attr) or f.get('name') or f.get('field')) not in (None, '*Unknown')]


IO = {'Input': 'input', 'Output': 'output'}


# --------------------------------------------------------------------------- converters (plugin short name -> fn)

def c_text_input(cfg):
    names = [f.get('name') for f in cfg.findall('Fields/Field')]
    data = [[(c.text if c.text is not None else '') for c in r.findall('c')] for r in cfg.findall('Data/r')]
    fields = []
    for i, n in enumerate(names):
        col = [r[i] for r in data if i < len(r)]
        vals = [v for v in col if v != '']
        if vals and all(re.fullmatch(r'[+-]?\d{1,9}', v) for v in vals):
            t = 'int32'
        elif vals and all(re.fullmatch(r'[+-]?\d+', v) for v in vals):
            t = 'int64'
        elif vals and all(re.fullmatch(r'[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?', v) for v in vals):
            t = 'double'
        else:
            t = 'v_wstring'
        spec = {'name': n, 'type': t}
        if t == 'v_wstring':  # size to the longest value (the 254 default would truncate)
            spec['size'] = max([len(v) for v in col] + [1])
        fields.append(spec)
    rows = [[None if v == '' and fields[i]['type'] != 'v_wstring' else v for i, v in enumerate(r)] for r in data]
    return Converted('core.text_input', {'fields': fields, 'rows': rows}, outputs={'Output': 'output'},
                     warnings=['Field types inferred from the text (integers/decimals/text); the source tool decides '
                               'types itself and may differ'])


def c_input_data(cfg):
    f = cfg.find('File')
    if f is not None and f.get('FileFormat') == '19':
        return Converted('core.input_data', {'path': (f.text or '').replace('\\', '/'), 'format': 'yxdb',
                                             'type_inference': 'alteryx-compat'},
                         outputs={'Output': 'output'},
                         warnings=['.yxdb read with coarse types (no original sizes/integer widths)'])
    if f is None or f.get('FileFormat') not in ('0', None):
        raise Unsupported(f'Input format {f.get("FileFormat") if f is not None else "?"} (only delimited text and yxdb)')
    path = (f.text or '').replace('\\', '/')
    opts = cfg.find('FormatSpecificOptions')
    delim = _t(opts, 'Delimeter', ',') if opts is not None else ','
    if delim == ',':
        fmt = {'format': 'csv'}
    elif delim in ('\\t', '\t'):
        fmt = {'format': 'tsv'}
    elif len(delim) == 1:
        fmt = {'format': 'delimited', 'delimiter': delim}
    else:
        raise Unsupported(f'Delimiter {delim!r}')
    conv = Converted('core.input_data', {'path': path, **fmt, 'type_inference': 'alteryx-compat',
                                         'has_header': _b(_t(opts, 'HeaderRow', 'True')) if opts is not None else True},
                     outputs={'Output': 'output'})
    if opts is not None and _t(opts, 'CodePage') not in (None, '65001', '28591', '1252'):
        conv.warnings.append(f"Code page {_t(opts, 'CodePage')} read as UTF-8")
    if opts is not None and _t(opts, 'CodePage') in ('28591', '1252'):
        conv.config['encoding'] = 'latin-1' if _t(opts, 'CodePage') == '28591' else 'cp1252'
    conv.warnings.append('Alteryx compatibility mode preserves delimited values as text; Flowbench automatic type '
                         'inference is available for native workflows. Source whitespace trimming may differ.')
    return conv


def c_output_data(cfg):
    f = cfg.find('File')
    if f is None or f.get('FileFormat') not in ('0', None):
        raise Unsupported(f'Output format {f.get("FileFormat") if f is not None else "?"} (only delimited text, format 0)')
    opts = cfg.find('FormatSpecificOptions')
    conf = {'path': (f.text or '').replace('\\', '/'), 'format': 'csv'}
    if opts is not None:
        conf['write_header'] = _b(_t(opts, 'HeaderRow', 'True'))
        conf['line_terminator'] = '\r\n' if _t(opts, 'LineEndStyle', 'CRLF') == 'CRLF' else '\n'
        d = _t(opts, 'Delimeter', ',')
        if d not in (',',):
            conf['format'], conf['delimiter'] = 'delimited', d
    return Converted('core.output_data', conf, inputs={'Input': 'input'})


def c_filter(cfg):
    expr = _t(cfg, 'Expression')
    if not expr:
        raise Unsupported('Filter without an expression')
    return Converted('core.filter', {'expression': expr}, inputs={'Input': 'input'},
                     outputs={'True': 'true', 'False': 'false'})


def c_formula(cfg):
    formulas = []
    for ff in cfg.findall('FormulaFields/FormulaField'):
        t = _type(ff.get('type'), 'Formula')
        formulas.append(_size({'field': ff.get('field'), 'expression': ff.get('expression') or '', 'type': t}, t, ff.get('size')))
    return Converted('core.formula', {'formulas': formulas}, inputs={'Input': 'input'}, outputs={'Output': 'output'})


def _select_fields(sel):
    fields, unknown = [], 'include'
    for sf in sel.findall('SelectFields/SelectField'):
        name = sf.get('field')
        if name == '*Unknown':
            unknown = 'include' if _b(sf.get('selected')) else 'exclude'
            continue
        spec = {'field': name, 'selected': _b(sf.get('selected', 'True'))}
        if sf.get('rename') and sf.get('rename') != name:
            spec['rename'] = sf.get('rename')
        if sf.get('type'):
            t = _type(sf.get('type'), 'Select')
            spec['type'] = t
            _size(spec, t, sf.get('size'))
        fields.append(spec)
    return fields, unknown


def c_select(cfg):
    fields, unknown = _select_fields(cfg)
    order = 'listed' if _b(_v(cfg, 'OrderChanged', 'False')) else 'input'
    return Converted('core.select', {'fields': fields, 'unknown_fields': unknown, 'missing_fields': 'ignore',
                                     'order': order},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_sort(cfg):
    keys = [{'field': f.get('field'), 'order': 'desc' if f.get('order') == 'Descending' else 'asc'}
            for f in cfg.findall('SortInfo/Field')]
    return Converted('core.sort', {'keys': keys}, inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_unique(cfg):
    return Converted('core.unique', {'fields': _fields(cfg, 'UniqueFields/Field')}, inputs={'Input': 'input'},
                     outputs={'Unique': 'unique', 'Duplicates': 'duplicates', 'Duplicate': 'duplicates'})


def c_sample(cfg):
    mode = _t(cfg, 'Mode')
    m = {'First': 'first', 'Last': 'last', 'Skip': 'skip', 'Sample': 'every_nth', 'NPercent': 'first_percent'}.get(mode)
    if not m:
        raise Unsupported(f'Sample mode {mode!r}')
    return Converted('core.sample', {'mode': m, 'n': int(_t(cfg, 'N', '0')), 'group_by': _fields(cfg, 'GroupFields/Field', 'name')},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_record_id(cfg):
    t = _type(_t(cfg, 'FieldType', 'Int32'), 'Record ID')
    conf = {'field_name': _t(cfg, 'FieldName', 'RecordID'), 'start': int(_t(cfg, 'StartValue', '1')),
            'position': 'last' if _t(cfg, 'Position', '0') == '1' else 'first'}
    if t in ('int16', 'int32', 'int64'):
        conf['type'] = t
    else:
        conf['type'] = 'v_string'
        conf['size'] = int(_t(cfg, 'FieldSize', '6'))
    return Converted('core.record_id', conf, inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_summarize(cfg):
    actions = []
    for sf in cfg.findall('SummarizeFields/SummarizeField'):
        op = SUMMARIZE_OPS.get(sf.get('action'))
        if not op:
            raise Unsupported(f"Summarize action {sf.get('action')!r}")
        a = {'field': sf.get('field'), 'op': op}
        if sf.get('rename'):
            a['output'] = sf.get('rename')
        if op == 'concat':
            a['separator'] = _t(cfg, 'Separator', ',') or ','
        actions.append(a)
    return Converted('core.summarize', {'actions': actions}, inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_join(cfg):
    conv = Converted('core.join', {}, inputs={'Left': 'left', 'Right': 'right'},
                     outputs={'Left': 'left_unjoined', 'Join': 'joined', 'Right': 'right_unjoined'})
    if _b(cfg.get('joinByRecordPos', 'False')):
        conv.config = {'by': 'position'}
    else:
        lk = _fields(cfg, "JoinInfo[@connection='Left']/Field")
        rk = _fields(cfg, "JoinInfo[@connection='Right']/Field")
        conv.config = {'by': 'fields', 'keys': [{'left': a, 'right': b} for a, b in zip(lk, rk)]}
    sel = cfg.find('SelectConfiguration/Configuration')
    if sel is not None:
        fields, unknown = _select_fields(sel)
        if any(f.get('rename') and not f['rename'].startswith('Right_') for f in fields) or \
                any('type' in f for f in fields) or unknown == 'exclude':
            conv.warnings.append('Join output field selection/renaming/retyping is not translated; add a Select tool')
        dropped = [f['field'] for f in fields if not f['selected']]
        if dropped:
            conv.warnings.append(f'Deselected join output fields kept (not translated): {dropped}')
    return conv


def c_union(cfg):
    mode = _t(cfg, 'Mode', 'ByName')
    if mode not in ('ByName', 'ByPos'):
        raise Unsupported(f'Union mode {mode!r} (manual field mapping)')
    return Converted('core.union', {'mode': 'name' if mode == 'ByName' else 'position',
                                    'missing_fields': 'intersection' if _t(cfg, 'ByName_OutputMode') == 'Subset' else 'include'},
                     inputs={'Input': 'inputs'}, outputs={'Output': 'output'})


def c_append(cfg):
    return Converted('core.append_fields', {'max_source_records': 1_000_000 if _t(cfg, 'CartesianMode') == 'Allow' else 16},
                     inputs={'Targets': 'target', 'Source': 'source'}, outputs={'Output': 'output'})


def c_running_total(cfg):
    return Converted('core.running_total', {'fields': _fields(cfg, 'RunningTotalFields/Field'),
                                            'group_by': _fields(cfg, 'GroupByFields/Field')},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_transpose(cfg):
    data = [f.get('field') for f in cfg.findall('DataFields/Field') if _b(f.get('selected')) and f.get('field') != '*Unknown']
    return Converted('core.transpose', {'key_fields': _fields(cfg, 'KeyFields/Field'), 'data_fields': data},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_cross_tab(cfg):
    names = {'Sum': 'sum', 'Count': 'count', 'Avg': 'avg', 'Min': 'min', 'Max': 'max', 'First': 'first', 'Last': 'last',
             'Concat': 'concat'}
    methods = [x.get('method') for x in cfg.findall('Methods/Method')] or ['Sum']
    bad = [x for x in methods if x not in names]
    if bad:
        raise Unsupported(f'Cross Tab method(s) {bad}')
    m = names[methods[0]]
    return Converted('core.cross_tab', {'group_by': _fields(cfg, 'GroupFields/Field'),
                                        'header_field': cfg.find('HeaderField').get('field'),
                                        'value_field': cfg.find('DataField').get('field'), 'method': m,
                                        **({'methods': [names[x] for x in methods]} if len(methods) > 1 else {}),
                                        'separator': _t(cfg, 'Methods/Separator', ',') or ','},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_text_to_columns(cfg):
    flags = _v(cfg, 'Flags', '0')
    if flags not in ('0', None):
        raise Unsupported(f'Text To Columns flags {flags} (split to rows / quote handling not mapped)')
    eh = _t(cfg, 'ErrorHandling', 'Last')
    return Converted('core.text_to_columns', {'field': _t(cfg, 'Field'), 'delimiters': _v(cfg, 'Delimeters', ','),
                                              'mode': 'columns', 'columns': int(_v(cfg, 'NumFields', '2')),
                                              'output_root': _t(cfg, 'RootName'),
                                              'extra': {'Last': 'last_column', 'Ignore': 'drop'}.get(eh, 'error')},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_regex(cfg):
    method = _t(cfg, 'Method')
    base = {'field': _t(cfg, 'Field'), 'pattern': _v(cfg, 'RegExExpression', ''),
            'case_insensitive': _b(_v(cfg, 'CaseInsensitve', 'False'))}
    conv = Converted('core.regex', base, inputs={'Input': 'input'}, outputs={'Output': 'output'})
    if method == 'Replace':
        base.update(mode='replace', replacement=cfg.find('Replace').get('expression', ''))
        if not _b(_v(cfg, 'Replace/CopyUnmatched', 'True')):
            conv.warnings.append('"Copy unmatched text" off is not translated')
    elif method == 'Match':
        base.update(mode='match', output_field=_t(cfg, 'Match/Field'))
    elif method == 'ParseComplex':
        base.update(mode='parse', group_fields=[f.get('field') for f in cfg.findall('ParseComplex/Field')])
    elif method == 'ParseSimple':
        rows = _b(_v(cfg, 'ParseSimple/SplitToRows', 'False'))
        base.update(mode='tokenize_rows' if rows else 'tokenize_columns', output_field=_t(cfg, 'ParseSimple/RootName'),
                    columns=int(_v(cfg, 'ParseSimple/NumFields', '3')))
    else:
        raise Unsupported(f'RegEx method {method!r}')
    return conv


def c_multi_row(cfg):
    if _t(cfg, 'OtherRows', 'Empty') not in ('Empty', 'NullData'):
        raise Unsupported(f"Multi-Row 'other rows' = {_t(cfg, 'OtherRows')!r}")
    update = _b(_v(cfg, 'UpdateField', 'False'))
    conf = {'expression': _t(cfg, 'Expression', ''), 'group_by': _fields(cfg, 'GroupByFields/Field'),
            'nonexistent_rows': 'zero_or_empty' if _t(cfg, 'OtherRows', 'Empty') == 'Empty' else 'null'}
    if update:
        conf['field'] = _t(cfg, 'UpdateField_Name')
    else:
        t = _type(_t(cfg, 'CreateField_Type'), 'Multi-Row Formula')
        conf.update(field=_t(cfg, 'CreateField_Name'), type=t)
        _size(conf, t, _t(cfg, 'CreateField_Size'))
    return Converted('core.multi_row_formula', conf, inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_generate_rows(cfg):
    if _b(_v(cfg, 'UpdateField', 'False')):
        raise Unsupported('Generate Rows updating an existing field')
    t = _type(_t(cfg, 'CreateField_Type'), 'Generate Rows')
    conf = {'field': _t(cfg, 'CreateField_Name'), 'type': t, 'init': _t(cfg, 'Expression_Init', ''),
            'condition': (_t(cfg, 'Expression_Cond', '') or '').strip(), 'loop': _t(cfg, 'Expression_Loop', '')}
    _size(conf, t, _t(cfg, 'CreateField_Size'))
    return Converted('core.generate_rows', conf, inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_browse(cfg):
    return Converted('core.browse', {}, inputs={'Input': 'input'})


def c_auto_field(cfg):
    sel = cfg.findall('Fields/Field')
    unknown = next((f for f in sel if f.get('field') == '*Unknown'), None)
    fields = None if unknown is not None and _b(unknown.get('selected')) else \
        [f.get('field') for f in sel if _b(f.get('selected')) and f.get('field') != '*Unknown']
    return Converted('core.auto_field', {'fields': fields}, inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_multi_field_formula(cfg):
    conf = {'fields': _fields(cfg, 'Fields/Field', 'name'), 'expression': _t(cfg, 'Expression', ''),
            'copy_output': _b(_v(cfg, 'CopyOutput', 'False'))}
    if conf['copy_output']:
        add, pos = _t(cfg, 'NewFieldAddOn', ''), _t(cfg, 'NewFieldAddOnPos', 'Suffix')
        conf['prefix'], conf['suffix'] = (add, '') if pos == 'Prefix' else ('', add)
    if _b(_v(cfg, 'ChangeFieldType', 'False')):
        ft = cfg.find('FieldType')
        if ft is not None and ft.get('type'):
            conf['output_type'] = _type(ft.get('type'), 'Multi-Field Formula')
    return Converted('core.multi_field_formula', conf, inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_tile(cfg):
    if _t(cfg, 'Method') != 'EqualRecords':
        raise Unsupported(f"Tile method {_t(cfg, 'Method')!r}")
    return Converted('core.tile', {'method': 'equal_records', 'tiles': int(_v(cfg, 'EqualRecords/NumTiles', '4')),
                                   'group_by': _fields(cfg, 'GroupFields/Field', 'name')},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_find_replace(cfg):
    mode = 'replace' if _t(cfg, 'ReplaceMode', 'Replace') == 'Replace' else 'append'
    conf = {'find_field': _t(cfg, 'FieldFind'), 'search_field': _t(cfg, 'FieldSearch'), 'mode': mode,
            'match': {'FindAny': 'any', 'FindBegin': 'beginning', 'FindWhole': 'entire'}.get(_t(cfg, 'FindMode'), 'any'),
            'case_insensitive': _b(_v(cfg, 'NoCase', 'False')), 'whole_word': _b(_v(cfg, 'MatchWholeWord', 'False')),
            'replace_all': _b(_v(cfg, 'ReplaceMultipleFound', 'True'))}
    if mode == 'replace':
        conf['replace_field'] = _t(cfg, 'ReplaceFoundField')
    else:
        conf['append_fields'] = _fields(cfg, 'ReplaceAppendFields/Field')
    return Converted('core.find_replace', conf, inputs={'Targets': 'find', 'Source': 'replace'}, outputs={'Output': 'output'})


def c_make_group(cfg):
    return Converted('core.make_group', {'key1': _t(cfg, 'Key1st'), 'key2': _t(cfg, 'Key2nd')},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_datetime(cfg):
    return Converted('core.datetime', {'direction': 'to_text' if _b(_v(cfg, 'IsFrom', 'False')) else 'to_date',
                                       'field': _t(cfg, 'InputFieldName'), 'format': _t(cfg, 'Format', 'yyyy-MM-dd'),
                                       'output_field': _t(cfg, 'OutputFieldName')},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_xml_parse(cfg):
    el = _t(cfg, 'XMLElement')
    return Converted('core.xml_parse', {'field': _t(cfg, 'XMLField'), 'element': el or None,
                                        'child_values': _b(_v(cfg, 'ChildValues', 'True')),
                                        'outer_xml': _b(_v(cfg, 'OuterXML', 'False')), 'attributes': False,
                                        'ignore_errors': _b(_v(cfg, 'IgnoreErrors', 'False'))},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_make_columns(cfg):
    return Converted('core.make_columns', {'columns': int(_t(cfg, 'NumColumns', '2')),
                                           'horizontal': _b(_v(cfg, 'IsHorizontal', 'True')),
                                           'group_by': _fields(cfg, 'GroupFields/Field', 'name')},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_field_info(cfg):
    return Converted('core.field_info', {}, inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_json_parse(cfg):
    return Converted('core.json_parse', {'field': _t(cfg, 'JSON_Field'),
                                         'include_input_fields': _b(_v(cfg, 'IncludeInOutput', 'False')),
                                         'single_field': _b(_v(cfg, 'SingleField', 'True'))},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def c_dynamic_rename(cfg):
    mode = _t(cfg, 'RenameMode')
    if mode != 'FirstRow':
        raise Unsupported(f'Dynamic Rename mode {mode!r}')
    return Converted('core.dynamic_rename', {'mode': 'first_row', 'fields': None},
                     inputs={'Targets': 'input', 'Source': 'names'}, outputs={'Output': 'output'})


def c_dynamic_select(cfg):
    if _t(cfg, 'Mode') == 'FieldTypes':
        types = [_type(x.strip(), 'Dynamic Select') for x in (_t(cfg, 'FieldTypes', '') or '').split(',') if x.strip()]
        return Converted('core.dynamic_select', {'mode': 'types', 'types': types},
                         inputs={'Input': 'input'}, outputs={'Output': 'output'})
    raise Unsupported(f"Dynamic Select mode {_t(cfg, 'Mode')!r}")


def c_fuzzy_match(cfg):
    """Translate the narrow, independently executable subset of Fuzzy Match XML.

    Vendor score aggregation, custom regex preprocessing, Japanese transforms and
    licensed word/nickname tables are deliberately rejected.  The imported XML is
    still retained verbatim by the normal importer for lossless unchanged export.
    """
    record_id = _t(cfg, 'RecordIdField')
    if not record_id:
        raise Unsupported('Fuzzy Match requires RecordIdField')
    merge = _b(_v(cfg, 'MergeMode', 'False'))
    source_id = _t(cfg, 'SourceIdField')
    if merge and not source_id:
        raise Unsupported('Fuzzy Match Merge mode requires SourceIdField')
    generated_only = _b(_v(cfg, 'GenerateKeys', 'False'))
    match_fields = []
    for item in cfg.findall('MatchFields/MatchField'):
        name = _t(item, 'FieldName')
        style = item.find('Style')
        if not name or style is None:
            raise Unsupported('Fuzzy Match MatchField requires FieldName and Style')
        if style.find('PreProcess') is not None:
            raise Unsupported('Fuzzy Match custom/vendor preprocessing is not translated')
        if style.find('Match/WordStats') is not None and (style.findtext('Match/WordStats') or '').strip():
            raise Unsupported('Fuzzy Match WordStats requires an Alteryx runtime dataset')
        if style.find('Match/NicknameFile') is not None:
            raise Unsupported('Fuzzy Match NicknameFile requires an Alteryx runtime dataset')
        if style.find('Match/NicknamePenalty') is not None:
            raise Unsupported('Fuzzy Match nickname scoring is not translated')
        key_fn = _t(style, 'KeyGeneration/KeyFunction', 'None')
        key_methods = {'DblMetaphone': 'double_metaphone', 'Soundex': 'soundex',
                       'DigitsOnly': 'digits_only', 'WholeFieldCI': 'whole_field_ci',
                       'Alphanumeric': 'alphanumeric', 'SoundexDigits': 'soundex_digits',
                       'DblMetaphoneDigits': 'double_metaphone_digits'}
        if key_fn not in key_methods:
            raise Unsupported(f'Fuzzy Match key function {key_fn!r} is not translated')
        match_fn = _t(style, 'Match/MatchFunction', 'None')
        match_methods = {'None': 'key_only', 'Levenshtein': 'levenshtein',
                         'Jaro': 'jaro', 'BestJaroLevenshtein': 'best_jaro_levenshtein'}
        if match_fn not in match_methods:
            raise Unsupported(f'Fuzzy Match function {match_fn!r} is not translated')
        additional = _t(style, 'Match/AdditionalMatchFunction', 'None')
        if additional not in ('None', ''):
            raise Unsupported(f'Fuzzy Match additional function {additional!r} is not translated')
        match = style.find('Match')
        kg = style.find('KeyGeneration')
        max_len = int(_v(kg, 'MaxKeyLen', '100')) if kg is not None else 100
        match_fields.append({
            'field': name, 'key_method': key_methods[key_fn],
            'per_word': _b(_v(kg, 'UseWords', 'False')) if kg is not None else False,
            'ignore_words': ( _t(kg, 'IgnoreWords', '') or '').split() if kg is not None else [],
            'ignore_single_letter_words': _b(_v(kg, 'IgnoreSingleLetterWords', 'False')) if kg is not None else False,
            'max_key_length': max_len, 'preprocess': 'none',
            'match_method': match_methods[match_fn],
            'granularity': 'character',
            'threshold': float(_v(match, 'MatchThreshold', '0')) if match is not None else 0,
            'weight': float(_v(match, 'MatchWeight', '100')) if match is not None else 100,
            'empty_policy': 'ignore' if _b(_v(kg, 'IgnoreEmpty', 'True')) else 'match',
        })
    if not match_fields:
        raise Unsupported('Fuzzy Match has no configured MatchField')
    conf = {
        'mode': 'merge' if merge else 'purge', 'record_id_field': record_id,
        'source_id_field': source_id or None,
        'match_threshold': float(_v(cfg, 'MatchThreshold', '80')),
        'match_fields': match_fields,
        'output_score': _b(_v(cfg, 'OutputScore', 'True')),
        'output_keys': _b(_v(cfg, 'OutputKey', 'False')),
        'output_unmatched': _b(_v(cfg, 'OutputUnmatched', 'False')),
        'output_groups': _b(_v(cfg, 'OutputMakeGroup', 'False')),
        'generate_keys_only': generated_only,
        'skip_pairs_in_same_match_group': False,
    }
    return Converted('core.fuzzy_match', conf, inputs={'Input': 'input'}, outputs={'Output': 'output'},
                     warnings=['Imported Alteryx options were translated to Flowbench v1 scoring; '
                               'pair scores and grouping have not been reference-verified'])


def c_macro_input(cfg):
    values = {v.get('name'): (v.text or '') for v in cfg.findall('Value')}
    converted = Converted('flowengine.macro_input', {'name': values.get('Name') or 'Input'},
                          outputs={'Output': 'output'})
    ignored = sorted(name for name, value in values.items() if name not in ('Name', 'Abbrev') and value.strip())
    nested = [el.tag.rsplit('}', 1)[-1] for el in cfg.iter()
              if el is not cfg and el.tag.rsplit('}', 1)[-1] != 'Value']
    if ignored or nested:
        converted.warnings.append('Macro Input options are not mapped: ' + ', '.join(sorted(set(ignored + nested))))
    return converted


def c_macro_output(cfg):
    values = {v.get('name'): (v.text or '') for v in cfg.findall('Value')}
    converted = Converted('flowengine.macro_output', {'name': values.get('Name') or 'Output'},
                          inputs={'Input': 'input'}, outputs={'Output': 'output'})
    ignored = sorted(name for name, value in values.items() if name not in ('Name', 'Abbrev') and value.strip())
    nested = [el.tag.rsplit('}', 1)[-1] for el in cfg.iter()
              if el is not cfg and el.tag.rsplit('}', 1)[-1] != 'Value']
    if ignored or nested:
        converted.warnings.append('Macro Output options are not mapped: ' + ', '.join(sorted(set(ignored + nested))))
    return converted


CONVERTERS = {
    'TextInput': c_text_input, 'DbFileInput': c_input_data, 'DbFileOutput': c_output_data, 'Filter': c_filter,
    'Formula': c_formula, 'AlteryxSelect': c_select, 'Sort': c_sort, 'Unique': c_unique, 'Sample': c_sample,
    'RecordID': c_record_id, 'Summarize': c_summarize, 'Join': c_join, 'Union': c_union, 'AppendFields': c_append,
    'RunningTotal': c_running_total, 'Transpose': c_transpose, 'CrossTab': c_cross_tab,
    'TextToColumns': c_text_to_columns, 'RegEx': c_regex, 'MultiRowFormula': c_multi_row,
    'GenerateRows': c_generate_rows, 'BrowseV2': c_browse, 'AutoField': c_auto_field,
    'MultiFieldFormula': c_multi_field_formula, 'Tile': c_tile, 'FindReplace': c_find_replace,
    'MakeGroup': c_make_group, 'DateTime': c_datetime, 'XMLParse': c_xml_parse, 'MakeColumns': c_make_columns,
    'FieldInfo': c_field_info, 'JSONParse': c_json_parse, 'DynamicRename': c_dynamic_rename,
    'DynamicSelect': c_dynamic_select,
    'FuzzyMatch': c_fuzzy_match,
    'MacroInput': c_macro_input,
    'MacroOutput': c_macro_output,
}


def _vals(cfg):
    return {v.get('name'): (v.text or '') for v in cfg.findall('Value')}


def _listbox(text):
    return [x.strip().strip('"') for x in (text or '').split(',') if x.strip().strip('"')]


def m_count_records(cfg):
    return Converted('core.count_records', {}, inputs={'Input8': 'input'}, outputs={'Output9': 'output'})


def m_weighted_avg(cfg):
    v = _vals(cfg)
    return Converted('core.weighted_average', {'value': v.get('Value'), 'weight': v.get('Weight'),
                                               'output_field': v.get('OutputFieldName') or 'WeightedAverage',
                                               'group_by': _listbox(v.get('GroupFields'))},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def m_select_records(cfg):
    return Converted('core.select_records', {'ranges': _vals(cfg).get('Ranges', '')},
                     inputs={'Input': 'input'}, outputs={'Output': 'output'})


def m_imputation(cfg):
    v = _vals(cfg)
    how = 'mean' if _b(v.get('radio Mean')) else 'median' if _b(v.get('radio Median')) else \
        'mode' if _b(v.get('radio Mode')) else 'value'
    conf = {'fields': _listbox(v.get('listbox Select Incoming Fields')), 'with': how,
            'replace': 'null' if _b(v.get('radio Null Value', 'True')) else 'value',
            'replace_value': float(v.get('updown User Replace Value') or 0),
            'with_value': float(v.get('updown User Replace With Value') or 0),
            'indicator': _b(v.get('checkbox Impute Indicator'))}
    conv = Converted('core.imputation', conf, inputs={'Input': 'input'}, outputs={'Output': 'output'})
    if _b(v.get('checkbox Imputed Values Separate Field')):
        conv.warnings.append('"Imputed values in separate field" is not translated')
    return conv


MACROS = {'countrecords.yxmc': m_count_records, 'weightedavg.yxmc': m_weighted_avg,
          'selectrecords.yxmc': m_select_records, 'imputation_v3.yxmc': m_imputation}


# --------------------------------------------------------------------------- import

def _identity(node) -> tuple[str | None, str | None]:
    gs = node.find('GuiSettings')
    plugin = gs.get('Plugin') if gs is not None else None
    es = node.find('EngineSettings')
    macro = es.get('Macro') if es is not None else None
    return plugin, macro


def _iter_nodes(parent, container=None):
    for node in parent.findall('Node'):
        yield node, container
        child = node.find('ChildNodes')
        if child is not None:
            yield from _iter_nodes(child, node.get('ToolID'))


def import_document(xml_text: str, *, name: str | None = None, source: str | None = None) -> tuple[dict, dict]:
    """Return (workflow document, import report)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f'Not a workflow XML document: {e}') from None
    if root.tag != 'AlteryxDocument':
        raise ValueError(f'Unexpected root element {root.tag!r}')
    version = root.get('yxmdVer')
    nodes, comments, containers = [], [], []
    anchor_maps: dict[str, Converted] = {}
    report_nodes = []
    original = {}
    child_map: dict[str, list[str]] = {}
    interface_tool_ids: set[str] = set()
    interface_xml = {}
    for node, container in _iter_nodes(root.find('Nodes') if root.find('Nodes') is not None else root):
        tid = node.get('ToolID')
        nid = f't{tid}'
        plugin, macro = _identity(node)
        short = (plugin or '').split('.')[-1]
        pos = node.find('GuiSettings/Position')
        position = {'x': float(pos.get('x', 0)), 'y': float(pos.get('y', 0))} if pos is not None else None
        cfg = node.find('Properties/Configuration')
        if container:
            child_map.setdefault(f't{container}', []).append(nid)
        if short == 'ToolContainer':
            size = node.find('GuiSettings/Position')
            containers.append({'id': nid, 'caption': _t(cfg, 'Caption', 'Container') if cfg is not None else 'Container',
                               'disabled': _b(_v(cfg, 'Disabled', 'False')) if cfg is not None else False,
                               'collapsed': _b(_v(cfg, 'Folded', 'False')) if cfg is not None else False,
                               'position': position or {'x': 0, 'y': 0},
                               'size': {'width': float(size.get('width', 320)) if size is not None else 320,
                                        'height': float(size.get('height', 200)) if size is not None else 200},
                               'parent': f't{container}' if container else None, 'children': []})
            report_nodes.append({'node': nid, 'source': plugin, 'status': 'mapped', 'as': 'container'})
            continue
        if short == 'TextBox':
            comments.append({'id': nid, 'text': _t(cfg, 'Text', '') if cfg is not None else '',
                             'position': position or {'x': 0, 'y': 0}, 'size': {'width': 200, 'height': 80},
                             'parent': f't{container}' if container else None})
            report_nodes.append({'node': nid, 'source': plugin, 'status': 'mapped', 'as': 'comment'})
            continue
        ident = plugin or macro or '(unknown)'
        # Question and Action nodes are application controls/configuration edges,
        # not row-processing operators. Their semantics are read from
        # RuntimeProperties below; never leak them into the ordinary data DAG.
        is_question = (plugin or '').startswith('AlteryxGuiToolkit.Questions.') and short not in ('MacroInput', 'MacroOutput')
        if plugin and (is_question or
                       plugin.startswith('AlteryxGuiToolkit.Condition.') or
                       plugin == 'AlteryxGuiToolkit.Action.Action'):
            interface_tool_ids.add(nid)
            interface_xml[nid] = ET.tostring(node, encoding='unicode')
            report_nodes.append({'node': nid, 'source': plugin, 'status': 'mapped',
                                 'as': ('application-question' if is_question else
                                        'application-condition' if '.Condition.' in plugin else 'application-action')})
            continue
        conv_fn = CONVERTERS.get(short) if plugin else MACROS.get(re.split(r'[\\/]', macro or '')[-1].lower())
        if not plugin and macro and conv_fn is None:
            # Preserve the macro reference for resolution relative to the parent workflow directory at run time.
            conv_fn = lambda _cfg, path=macro: Converted('flowengine.macro', {'definition_path': path},
                                                           inputs={'Input': 'input'}, outputs={'Output': 'output'})
        entry = {'node': nid, 'source': ident}
        try:
            if conv_fn is None or cfg is None:
                raise Unsupported('no converter for this tool' if conv_fn is None else 'node has no configuration')
            conv = conv_fn(cfg)
            anchor_maps[nid] = conv
            node_doc = {'id': nid, 'tool': conv.tool, 'config': conv.config}
            entry.update(status='partial' if conv.warnings else 'mapped', tool=conv.tool, warnings=conv.warnings)
            original[nid] = {'xml': ET.tostring(node, encoding='unicode'), 'config_digest': _digest(conv.config)}
        except Unsupported as e:
            node_doc = {'id': nid, 'tool': ident, 'config': {}}
            entry.update(status='unsupported', reason=str(e))
            original[nid] = {'xml': ET.tostring(node, encoding='unicode'), 'config_digest': None}
        except (ValueError, TypeError, AttributeError) as e:
            node_doc = {'id': nid, 'tool': ident, 'config': {}}
            entry.update(status='unsupported', reason=f'configuration could not be read: {e}')
            original[nid] = {'xml': ET.tostring(node, encoding='unicode'), 'config_digest': None}
        if position:
            node_doc['position'] = position
        ann = node.find('Properties/Annotation')
        if ann is not None and (_t(ann, 'AnnotationText') or '').strip():
            node_doc['annotation'] = _t(ann, 'AnnotationText').strip()
        if ann is not None and _b(ann.get('DisableTool', 'False')):
            node_doc['disabled'] = True
        nodes.append(node_doc)
        report_nodes.append(entry)
    for c in containers:
        c['children'] = child_map.get(c['id'], [])
    edges = []
    edge_report = []
    conns = root.findall('Connections/Connection')
    named = sorted(conns, key=lambda c: (int(re.sub(r'\D', '', c.get('name') or '') or 0)))
    for conn in named:
        o, d = conn.find('Origin'), conn.find('Destination')
        src, dst = f"t{o.get('ToolID')}", f"t{d.get('ToolID')}"
        so, di = o.get('Connection'), d.get('Connection')
        if dst in interface_tool_ids and di == 'Question' and src not in interface_tool_ids:
            # Condition/Control Parameter paths are deliberately not inferred as
            # ordinary questions. The supported first slice is UI question -> Action.
            continue
        if src in interface_tool_ids and src not in anchor_maps and so == 'Action':
            continue
        if src in interface_tool_ids or dst in interface_tool_ids:
            continue
        s_anchor = anchor_maps[src].outputs.get(so, so) if src in anchor_maps else so
        d_anchor = anchor_maps[dst].inputs.get(di, di) if dst in anchor_maps else di
        if src in anchor_maps and so not in anchor_maps[src].outputs:
            edge_report.append(f'{src}.{so} -> {dst}: output anchor not recognised')
        if dst in anchor_maps and di not in anchor_maps[dst].inputs:
            edge_report.append(f'{src} -> {dst}.{di}: input anchor not recognised (e.g. interface "Question"/"Action")')
        edges.append({'from': {'node': src, 'anchor': s_anchor}, 'to': {'node': dst, 'anchor': d_anchor}})
    application, action_warnings = _import_alteryx_application(root, interface_tool_ids, conns, nodes)
    safe_runtime_xml, redacted_secret = _safe_runtime_properties(root)
    if redacted_secret:
        action_warnings.append('Password question defaults were redacted from imported RuntimeProperties and will not be exported')
    edge_report.extend(action_warnings)
    counts = {}
    for n in report_nodes:
        counts[n['status']] = counts.get(n['status'], 0) + 1
    preserved_interface_connections = [ET.tostring(c, encoding='unicode') for c in conns
                                        if c.find('Origin') is not None and c.find('Destination') is not None
                                        and (f"t{c.find('Origin').get('ToolID')}" in interface_tool_ids or
                                             f"t{c.find('Destination').get('ToolID')}" in interface_tool_ids)]
    metadata = {'ui': {'containers': containers, 'comments': comments},
                                        'import': {'source': source, 'source_format_version': version,
                                                   'nodes': original,
                                                   'interface_nodes': interface_xml,
                                                   'interface_connections': preserved_interface_connections,
                                                   'runtime_properties': safe_runtime_xml}}
    if application['questions'] or application['actions']:
        metadata['application'] = application
    doc = {'format': 'flowengine.workflow', 'formatVersion': 1, 'name': name or 'Imported workflow', 'nodes': nodes,
           'edges': edges, 'metadata': metadata}
    report = {'source': source, 'source_format_version': version, 'counts': counts, 'nodes': report_nodes,
              'edge_warnings': edge_report}
    return doc, report


def _safe_runtime_properties(root):
    runtime = root.find('Properties/RuntimeProperties')
    if runtime is None:
        return None, False
    cloned = ET.fromstring(ET.tostring(runtime, encoding='unicode'))
    redacted = False
    for question in cloned.findall('.//Questions/Question'):
        password = question.find('Password')
        if password is None or not _b(password.get('value', password.text or 'False')):
            continue
        default = question.find('Default')
        if default is not None:
            default.text = None
            default.attrib.pop('value', None)
            redacted = True
    return ET.tostring(cloned, encoding='unicode'), redacted


def _import_alteryx_application(root, interface_tool_ids, connections, nodes):
    """Translate only reference-observed, safely representable app actions.

    Initial supported path: a question connected to an Action whose RuntimeProperties
    entry is UpdateValue/Simple and targets a Filter Expression. Other variants are
    surfaced as import warnings, not approximated.
    """
    rp = root.find('Properties/RuntimeProperties')
    if rp is None:
        return {'questions': [], 'actions': []}, []
    warnings = []
    qs = {}
    for q in rp.findall('.//Questions/Question'):
        tid = _v(q, 'ToolId')
        if not tid:
            continue
        qtype = _t(q, 'Type', '')
        typ = {'TextBox': 'text', 'NumericUpDown': 'number', 'CheckBox': 'boolean',
               'DropDown': 'choice', 'ListBox': 'multi-choice', 'RadioButton': 'choice',
               'Date': 'date', 'FileBrowse': 'file', 'FolderBrowse': 'folder',
               'CheckBoxGroup': 'multi-choice'}.get(qtype)
        if not typ:
            warnings.append(f'Alteryx application question {tid} ({qtype or "unknown"}) is preserved but not exposed in the Flowbench run form')
            continue
        value_el = q.find('Default')
        default = None
        if value_el is not None:
            raw = value_el.get('value') if value_el.get('value') is not None else value_el.text
            if raw not in (None, ''):
                try:
                    default = float(raw) if typ == 'number' else (raw.strip().lower() == 'true' if typ == 'boolean' else raw)
                except ValueError:
                    default = None
        options = []
        for option in q.findall('.//Option') + q.findall('.//Item'):
            value = option.get('value') or option.get('name') or (option.text or '').strip()
            if value and value not in options:
                options.append(value)
        if typ in ('choice', 'multi-choice') and not options:
            # Imported choices without a recoverable option list remain preserved in XML,
            # but cannot safely be prompted or bound.
            warnings.append(f'Alteryx application question {tid} ({qtype}) has no recoverable options and is preserved but not exposed')
            continue
        secret = _b(_v(q, 'Password', 'False'))
        qs[str(tid)] = {'id': f'alteryx-question-{tid}', 'label': _t(q, 'Description', '') or _t(q, 'Name', f'Question {tid}'),
                        'type': typ, 'required': False, 'secret': secret,
                        **({'options': options} if options else {}),
                        **({'default': default} if default is not None and not secret else {})}
    source_questions = {}
    for c in connections:
        o, d = c.find('Origin'), c.find('Destination')
        if o is not None and d is not None and d.get('Connection') == 'Question' and o.get('ToolID') in qs:
            source_questions.setdefault(d.get('ToolID'), []).append(o.get('ToolID'))
    node_by_id = {n['id'][1:]: n for n in nodes}
    parsed_actions = []
    for action in rp.findall('.//Actions/NoCondition/True/Action'):
        atid = _v(action, 'ToolId')
        targets = [c.find('Destination').get('ToolID') for c in connections
                   if c.find('Origin') is not None and c.find('Destination') is not None
                   and c.find('Origin').get('ToolID') == atid and c.find('Origin').get('Connection') == 'Action']
        connected = source_questions.get(str(atid), [])
        typ, mode, destination = _t(action, 'Type'), _t(action, 'Mode'), _t(action, 'Destination', '')
        # Runtime action ToolId is the Action control; target tool id is encoded
        # in the destination prefix and is independently checked against its edge.
        match = re.match(r'^(\d+)/(.+)$', destination)
        dest_tool = match.group(1) if match else None
        if typ == 'UpdateValue' and mode == 'Simple' and len(connected) == 1 and len(targets) == 1 and dest_tool == targets[0]:
            target_node = node_by_id.get(dest_tool)
            destination_path = match.group(2)
            path = None
            if target_node and target_node['tool'] == 'core.filter' and destination_path == 'Expression':
                path = ['expression']
            elif target_node and target_node['tool'] == 'core.text_input':
                cell = re.fullmatch(r'Text\s+Input\s+Data/r\[(\d+)\]/c\[(\d+)\]', destination_path)
                if cell:
                    # Designer RuntimeProperties uses one-based row/cell indices.
                    row, col = int(cell.group(1)) - 1, int(cell.group(2)) - 1
                    rows = target_node.get('config', {}).get('rows', [])
                    fields = target_node.get('config', {}).get('fields', [])
                    if row < len(rows) and col < len(fields):
                        path = ['rows', str(row), str(col)]
            elif target_node and target_node['tool'] == 'core.sort' and re.fullmatch(r'SortInfo/Field(?:\[\d+\])?/@order', destination_path):
                if len(target_node.get('config', {}).get('keys', [])) == 1:
                    path = ['keys', '0', 'order']
            elif (target_node and target_node['tool'] == 'core.formula'
                  and destination_path == 'FormulaFields/FormulaField/@expression'
                  and len(target_node.get('config', {}).get('formulas', [])) == 1):
                # The installed Designer sample uses this path for a Formula tool
                # with one FormulaField. Multiple formula fields are ambiguous
                # because the serialized destination does not identify a row.
                path = ['formulas', '0', 'expression']
            if path:
                parsed_actions.append({'question': f'alteryx-question-{connected[0]}', 'node_id': target_node['id'],
                                       'config_path': path, 'alteryx': {'type': typ, 'mode': mode,
                                       'destination': destination, 'branch': 'true'}})
                continue
        warnings.append(f"Alteryx Action {atid or '?'} ({typ or 'unknown'}/{mode or 'unknown'}) is preserved as unsupported; destination/target/question mapping is not supported")
    if rp.findall('.//Actions/Condition'):
        warnings.append('Condition-driven Action execution is preserved as unsupported; condition evaluation has not been adapted')
    referenced_action_ids = {str(_v(a, 'ToolId')) for a in rp.findall('.//Actions//Action') if _v(a, 'ToolId')}
    node_parent = root.find('Nodes')
    for node, _container in _iter_nodes(node_parent if node_parent is not None else root):
        gs = node.find('GuiSettings')
        if gs is not None and gs.get('Plugin') == 'AlteryxGuiToolkit.Action.Action':
            tid = node.get('ToolID')
            if tid not in referenced_action_ids:
                warnings.append(f'Alteryx Action node {tid} has no matching RuntimeProperties rule and is not executable')
    return {'questions': list(qs.values()), 'actions': parsed_actions}, warnings


def _digest(cfg) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def import_file(path: Path) -> tuple[dict, dict]:
    path = Path(path)
    ext = path.suffix.lower()
    if ext == '.yxzp':
        with zipfile.ZipFile(path) as z:
            mains = [n for n in z.namelist() if n.lower().endswith(('.yxmd', '.yxwz', '.yxmc')) and '/' not in n.strip('/')]
            mains = mains or [n for n in z.namelist() if n.lower().endswith(('.yxmd', '.yxwz', '.yxmc'))]
            if not mains:
                raise ValueError('Package contains no workflow document')
            text = z.read(mains[0]).decode('utf-8-sig')
            doc, report = import_document(text, name=Path(mains[0]).stem, source=f'{path.name}!{mains[0]}')
            report['package_members'] = sorted(z.namelist())
            return doc, report
    if ext not in ('.yxmd', '.yxmc', '.yxwz'):
        raise ValueError(f'Unsupported file type {ext!r}')
    doc, report = import_document(path.read_text(encoding='utf-8-sig'), name=path.stem, source=path.name)
    if ext == '.yxmc':
        report['notes'] = ['Macro definition imported as a workflow. Supported Macro Input/Output boundaries can be '
                           'executed through a Flowbench macro call; unsupported child tools remain explicit.']
    if ext == '.yxwz':
        report['notes'] = ['Analytic app imported as a workflow. Supported Action variants are applied to a private run '
                           'copy; unsupported Interface actions and question types are listed in edge_warnings and '
                           'their original XML is preserved.']
    return doc, report


# --------------------------------------------------------------------------- export

def _node_xml(tid, plugin, entry, cfg_xml, pos, macro=None):
    p = f'<Position x="{int(pos.get("x", 0))}" y="{int(pos.get("y", 0))}" />'
    if macro:
        return (f'<Node ToolID="{tid}"><GuiSettings>{p}</GuiSettings><Properties><Configuration>{cfg_xml}</Configuration>'
                f'</Properties><EngineSettings Macro={quoteattr(macro)} /></Node>')
    dll = 'AlteryxSpatialPluginsEngine.dll' if plugin.startswith('AlteryxSpatial') else 'AlteryxBasePluginsEngine.dll'
    return (f'<Node ToolID="{tid}"><GuiSettings Plugin={quoteattr(plugin)}>{p}</GuiSettings><Properties>'
            f'<Configuration>{cfg_xml}</Configuration></Properties>'
            f'<EngineSettings EngineDll="{dll}" EngineDllEntryPoint="{entry}" /></Node>')


def _x_text_input(c):
    fields = ''.join(f'<Field name={quoteattr(f["name"])} />' for f in c['fields'])
    data = ''.join('<r>' + ''.join('<c />' if v is None else f'<c>{escape(str(v))}</c>' for v in r) + '</r>' for r in c['rows'])
    return 'AlteryxBasePluginsGui.TextInput.TextInput', 'AlteryxTextInput', \
        f'<NumRows value="{len(c["rows"])}" /><Fields>{fields}</Fields><Data>{data}</Data>', {}, {'output': 'Output'}


def _x_filter(c):
    return 'AlteryxBasePluginsGui.Filter.Filter', 'AlteryxFilter', \
        f'<Expression>{escape(c["expression"])}</Expression><Mode>Custom</Mode>', {'input': 'Input'}, \
        {'true': 'True', 'false': 'False'}


def _x_formula(c):
    body = ''.join(f'<FormulaField expression={quoteattr(f["expression"])} field={quoteattr(f["field"])} '
                   f'size="{f.get("size", 1073741823 if f.get("type", "v_wstring").endswith("string") else 8)}" '
                   f'type="{TYPE_TO_REF[f.get("type", "v_wstring")]}" />' for f in c['formulas'])
    return 'AlteryxBasePluginsGui.Formula.Formula', 'AlteryxFormula', f'<FormulaFields>{body}</FormulaFields>', \
        {'input': 'Input'}, {'output': 'Output'}


def _x_sort(c):
    body = ''.join(f'<Field field={quoteattr(k["field"])} order="{"Descending" if k.get("order") == "desc" else "Ascending"}" />'
                   for k in c['keys'])
    return 'AlteryxBasePluginsGui.Sort.Sort', 'AlteryxSort', f'<SortInfo locale="0">{body}</SortInfo>', \
        {'input': 'Input'}, {'output': 'Output'}


def _x_summarize(c):
    inv = {v: k for k, v in SUMMARIZE_OPS.items()}
    body = ''.join(f'<SummarizeField field={quoteattr(a["field"])} action="{inv[a["op"]]}" '
                   f'rename={quoteattr(a.get("output") or a["field"])} />' for a in c['actions'])
    return 'AlteryxSpatialPluginsGui.Summarize.Summarize', 'AlteryxSummarize', \
        f'<SummarizeFields>{body}</SummarizeFields>', {'input': 'Input'}, {'output': 'Output'}


def _x_unique(c):
    body = ''.join(f'<Field field={quoteattr(f)} />' for f in c['fields'])
    return 'AlteryxBasePluginsGui.Unique.Unique', 'AlteryxUnique', f'<UniqueFields>{body}</UniqueFields>', \
        {'input': 'Input'}, {'unique': 'Unique', 'duplicates': 'Duplicates'}


def _x_browse(c):
    return 'AlteryxBasePluginsGui.BrowseV2.BrowseV2', 'AlteryxBrowseV2', '', {'input': 'Input'}, {}


EXPORTERS = {'core.text_input': _x_text_input, 'core.filter': _x_filter, 'core.formula': _x_formula,
             'core.sort': _x_sort, 'core.summarize': _x_summarize, 'core.unique': _x_unique, 'core.browse': _x_browse}


def export_document(doc: dict) -> tuple[str, list[str]]:
    """Return (XML text, problems). Problems list nodes that cannot be expressed; nothing is dropped silently:
    if problems is non-empty the caller must not treat the export as complete."""
    imported = doc.get('metadata', {}).get('import', {})
    original = imported.get('nodes', {})
    interface_nodes = imported.get('interface_nodes', {})
    interface_connections = imported.get('interface_connections', [])
    ids = {n['id']: i + 1 for i, n in enumerate(doc['nodes'])}
    old_to_new = {n['id'][1:]: ids[n['id']] for n in doc['nodes'] if re.fullmatch(r't\d+', n['id'])}
    for offset, old_id in enumerate(interface_nodes, start=len(ids) + 1):
        old_to_new[old_id[1:] if old_id.startswith('t') else old_id] = offset
    maps, xml_nodes, problems = {}, [], []
    for n in doc['nodes']:
        tid = ids[n['id']]
        o = original.get(n['id'])
        preserve_unsupported_original = False
        if o and o.get('config_digest') is None and not n.get('config'):
            try:
                source_el = ET.fromstring(o['xml'])
                source_plugin, _ = _identity(source_el)
                preserve_unsupported_original = bool(source_plugin and source_plugin == n['tool'])
            except (ET.ParseError, KeyError):
                pass
        if o and (o.get('config_digest') == _digest(n['config']) or preserve_unsupported_original):
            # unchanged imported node: emit the original XML, renumbered
            el = ET.fromstring(o['xml'])
            el.set('ToolID', str(tid))
            for ch in el.findall('ChildNodes'):
                el.remove(ch)
            xml_nodes.append(ET.tostring(el, encoding='unicode'))
            short = ((el.find('GuiSettings').get('Plugin') if el.find('GuiSettings') is not None else '') or '').split('.')[-1]
            fn = CONVERTERS.get(short)
            try:
                conv = fn(el.find('Properties/Configuration')) if fn else None
            except Unsupported as exc:
                conv = None
                problems.append(f"{n['id']}: original {short} settings retained but anchors are not translated ({exc})")
            maps[n['id']] = ({v: k for k, v in conv.inputs.items()}, {v: k for k, v in conv.outputs.items()}) if conv else ({}, {})
            continue
        fn = EXPORTERS.get(n['tool'])
        if not fn:
            problems.append(f"{n['id']}: {n['tool']} has no export mapping")
            continue
        plugin, entry, cfg_xml, ins, outs = fn(n['config'])
        xml_nodes.append(_node_xml(tid, plugin, entry, cfg_xml, n.get('position') or {}))
        maps[n['id']] = (ins, outs)
    for old_id, raw in interface_nodes.items():
        try:
            el = ET.fromstring(raw)
            el.set('ToolID', str(old_to_new[old_id[1:] if old_id.startswith('t') else old_id]))
            xml_nodes.append(ET.tostring(el, encoding='unicode'))
        except (ET.ParseError, KeyError) as exc:
            problems.append(f'{old_id}: imported interface node cannot be restored ({exc})')
    conns = []
    multi: dict = {}
    for e in doc['edges']:
        s, d = e['from'], e['to']
        if s['node'] not in maps or d['node'] not in maps:
            continue
        so = maps[s['node']][1].get(s['anchor'])
        di = maps[d['node']][0].get(d['anchor'])
        if so is None or di is None:
            problems.append(f"edge {s['node']}.{s['anchor']} -> {d['node']}.{d['anchor']} has no export mapping")
            continue
        k = multi[(d['node'], di)] = multi.get((d['node'], di), 0) + 1
        conns.append((ids[s['node']], so, ids[d['node']], di, k))
    totals = {}
    for c in conns:
        totals[(c[2], c[3])] = totals.get((c[2], c[3]), 0) + 1
    conn_xml_extra = []
    for raw in interface_connections:
        try:
            el = ET.fromstring(raw)
            for endpoint in (el.find('Origin'), el.find('Destination')):
                if endpoint is None or endpoint.get('ToolID') not in old_to_new:
                    raise KeyError(endpoint.get('ToolID') if endpoint is not None else 'endpoint')
                endpoint.set('ToolID', str(old_to_new[endpoint.get('ToolID')]))
            conn_xml_extra.append(ET.tostring(el, encoding='unicode'))
        except (ET.ParseError, KeyError) as exc:
            problems.append(f'imported interface connection cannot be restored ({exc})')
    conn_xml = ''.join(
        f'<Connection{f" name={quoteattr(chr(35) + str(k))}" if totals[(dn, di)] > 1 else ""}><Origin ToolID="{sn}" '
        f'Connection="{so}" /><Destination ToolID="{dn}" Connection="{di}" /></Connection>'
        for sn, so, dn, di, k in conns) + ''.join(conn_xml_extra)
    runtime_xml = ''
    if imported.get('runtime_properties'):
        try:
            rt = ET.fromstring(imported['runtime_properties'])
            for el in rt.iter():
                if el.tag.lower() == 'toolid' and el.get('value') in old_to_new:
                    el.set('value', str(old_to_new[el.get('value')]))
                if el.tag == 'Destination' and el.text:
                    match = re.match(r'^(\d+)(/.*)$', el.text, flags=re.S)
                    if match and match.group(1) in old_to_new:
                        el.text = str(old_to_new[match.group(1)]) + match.group(2)
            runtime_xml = ET.tostring(rt, encoding='unicode')
        except ET.ParseError as exc:
            problems.append(f'imported RuntimeProperties cannot be restored ({exc})')
    text = ('<?xml version="1.0"?>\n<AlteryxDocument yxmdVer="2026.1"><Nodes>' + ''.join(xml_nodes) + '</Nodes>'
            f'<Connections>{conn_xml}</Connections><Properties><Memory default="True" /><GlobalRecordLimit value="0" />'
            '<TempFiles default="True" /><RunWithE2 value="False" />' + runtime_xml + '</Properties></AlteryxDocument>\n')
    return text, problems
