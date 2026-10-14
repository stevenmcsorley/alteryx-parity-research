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
        return Converted('core.input_data', {'path': (f.text or '').replace('\\', '/'), 'format': 'yxdb'},
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
    conv = Converted('core.input_data', {'path': path, **fmt,
                                         'has_header': _b(_t(opts, 'HeaderRow', 'True')) if opts is not None else True},
                     outputs={'Output': 'output'})
    if opts is not None and _t(opts, 'CodePage') not in (None, '65001', '28591', '1252'):
        conv.warnings.append(f"Code page {_t(opts, 'CodePage')} read as UTF-8")
    if opts is not None and _t(opts, 'CodePage') in ('28591', '1252'):
        conv.config['encoding'] = 'latin-1' if _t(opts, 'CodePage') == '28591' else 'cp1252'
    conv.warnings.append('Delimited text is read as text fields (source tool reads V_String size 254 and trims '
                         'unquoted whitespace; flowengine keeps values unbounded and untrimmed)')
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
    'MacroInput': lambda cfg: Converted('flowengine.macro_input',
        {'name': _vals(cfg).get('Name') or _t(cfg, 'Name', 'Input')}, outputs={'Output': 'output'}),
    'MacroOutput': lambda cfg: Converted('flowengine.macro_output',
        {'name': _vals(cfg).get('Name') or _t(cfg, 'Name', 'Output')},
        inputs={'Input': 'input'}, outputs={'Output': 'output'}),
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
        s_anchor = anchor_maps[src].outputs.get(so, so) if src in anchor_maps else so
        d_anchor = anchor_maps[dst].inputs.get(di, di) if dst in anchor_maps else di
        if src in anchor_maps and so not in anchor_maps[src].outputs:
            edge_report.append(f'{src}.{so} -> {dst}: output anchor not recognised')
        if dst in anchor_maps and di not in anchor_maps[dst].inputs:
            edge_report.append(f'{src} -> {dst}.{di}: input anchor not recognised (e.g. interface "Question"/"Action")')
        edges.append({'from': {'node': src, 'anchor': s_anchor}, 'to': {'node': dst, 'anchor': d_anchor}})
    counts = {}
    for n in report_nodes:
        counts[n['status']] = counts.get(n['status'], 0) + 1
    doc = {'format': 'flowengine.workflow', 'formatVersion': 1, 'name': name or 'Imported workflow', 'nodes': nodes,
           'edges': edges, 'metadata': {'ui': {'containers': containers, 'comments': comments},
                                        'import': {'source': source, 'source_format_version': version,
                                                   'nodes': original}}}
    report = {'source': source, 'source_format_version': version, 'counts': counts, 'nodes': report_nodes,
              'edge_warnings': edge_report}
    return doc, report


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
        report['notes'] = ['Analytic app imported as a workflow; interface tools are preserved but the app runtime is '
                           'not implemented yet']
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
    original = doc.get('metadata', {}).get('import', {}).get('nodes', {})
    ids = {n['id']: i + 1 for i, n in enumerate(doc['nodes'])}
    maps, xml_nodes, problems = {}, [], []
    for n in doc['nodes']:
        tid = ids[n['id']]
        o = original.get(n['id'])
        if o and o.get('config_digest') == _digest(n['config']):
            # unchanged imported node: emit the original XML, renumbered
            el = ET.fromstring(o['xml'])
            el.set('ToolID', str(tid))
            for ch in el.findall('ChildNodes'):
                el.remove(ch)
            xml_nodes.append(ET.tostring(el, encoding='unicode'))
            short = ((el.find('GuiSettings').get('Plugin') if el.find('GuiSettings') is not None else '') or '').split('.')[-1]
            fn = CONVERTERS.get(short)
            conv = fn(el.find('Properties/Configuration')) if fn else None
            maps[n['id']] = ({v: k for k, v in conv.inputs.items()}, {v: k for k, v in conv.outputs.items()}) if conv else ({}, {})
            continue
        fn = EXPORTERS.get(n['tool'])
        if not fn:
            problems.append(f"{n['id']}: {n['tool']} has no export mapping")
            continue
        plugin, entry, cfg_xml, ins, outs = fn(n['config'])
        xml_nodes.append(_node_xml(tid, plugin, entry, cfg_xml, n.get('position') or {}))
        maps[n['id']] = (ins, outs)
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
    conn_xml = ''.join(
        f'<Connection{f" name={quoteattr(chr(35) + str(k))}" if totals[(dn, di)] > 1 else ""}><Origin ToolID="{sn}" '
        f'Connection="{so}" /><Destination ToolID="{dn}" Connection="{di}" /></Connection>'
        for sn, so, dn, di, k in conns)
    text = ('<?xml version="1.0"?>\n<AlteryxDocument yxmdVer="2026.1"><Nodes>' + ''.join(xml_nodes) + '</Nodes>'
            f'<Connections>{conn_xml}</Connections><Properties><Memory default="True" /><GlobalRecordLimit value="0" />'
            '<TempFiles default="True" /><RunWithE2 value="False" /></Properties></AlteryxDocument>\n')
    return text, problems
