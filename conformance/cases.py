"""Conformance cases: original synthetic inputs + equivalent configurations for
flowengine and for the reference engine's workflow format.

Reference configuration XML is authored here by the project, using element and
enumeration names observed structurally (data/reference-observations.json).
Every input value is invented for this project.
"""
from xml.sax.saxutils import escape, quoteattr

BASE = 'AlteryxBasePluginsEngine.dll'


def ref(plugin_short, configuration, inputs=None, entry=None, plugin=None, dll=BASE):
    return {'plugin': plugin or f'AlteryxBasePluginsGui.{plugin_short}.{plugin_short}',
            'engine_dll': dll, 'entry_point': entry or f'Alteryx{plugin_short}',
            'configuration': f'<Configuration>{configuration}</Configuration>',
            'input_anchors': inputs or {'input': 'Input'}}


def F(name, type_, **kw):
    return {'name': name, 'type': type_, **kw}


def fields_xml(tag, names, **attrs):
    extra = ''.join(f' {k}={quoteattr(v)}' for k, v in attrs.items())
    return ''.join(f'<{tag} field={quoteattr(n)}{extra} />' for n in names)


# --------------------------------------------------------------------------- shared inputs
PEOPLE = {'fields': [F('id', 'int32'), F('name', 'v_string', size=40), F('region', 'v_string', size=10),
                     F('score', 'double'), F('joined', 'date')],
          'rows': [[1, 'Ada', 'North', 75.5, '2024-01-31'],
                   [2, 'ben', 'South', None, '2024-02-29'],
                   [3, 'Cy', 'North', 92.0, '2023-12-15'],
                   [4, 'Di', None, 0.0, None],
                   [5, 'Ed', 'South', -5.25, '2024-03-01'],
                   [6, 'ADA', 'north', 75.5, '2024-01-31']]}

SALES = {'fields': [F('region', 'v_string', size=10), F('product', 'v_string', size=10),
                    F('qty', 'int32'), F('amount', 'double')],
         'rows': [['N', 'x', 1, 10.5], ['S', 'y', None, 2.0], ['N', 'y', 3, 3.25], ['N', 'x', 4, None],
                  ['E', 'z', 2, 7.0], ['S', 'x', 5, 1.5]]}

TEXT = {'fields': [F('id', 'int32'), F('s', 'v_wstring', size=60)],
        'rows': [[1, 'john smith, 42'], [2, '  padded  '], [3, None], [4, 'a,b,c,d'], [5, ''], [6, 'Zoë Ünïcode']]}

CASES = []


def case(case_id, catalog_id, description, inputs, fe_tool, fe_config, reference, outputs, **kw):
    CASES.append({'case_id': case_id, 'catalog_id': catalog_id, 'description': description, 'inputs': inputs,
                  'flowengine': {'tool': fe_tool, 'config': fe_config}, 'reference': reference,
                  'outputs': outputs, **kw})


# --------------------------------------------------------------------------- Filter
def filter_ref(expr):
    return ref('Filter', f'<Expression>{escape(expr)}</Expression><Mode>Custom</Mode>')


for cid, expr, desc in [
    ('filter-numeric-gt-null', '[score] > 10', 'numeric comparison; null score must not pass'),
    ('filter-string-eq-case', '[name] = "Ada"', 'string equality case sensitivity'),
    ('filter-compound-null', '[region] = "South" OR IsNull([region])', 'OR with IsNull'),
    ('filter-date-literal', '[joined] < "2024-02-01"', 'date compared with ISO string literal; null date'),
    ('filter-not-in', '!([region] IN ("North", "South"))', 'IN list with NOT and null operand'),
]:
    case(cid, 'preparation:filter', desc, {'input': PEOPLE}, 'core.filter', {'expression': expr}, filter_ref(expr),
         {'true': 'True', 'false': 'False'})


# --------------------------------------------------------------------------- Formula
def formula_ref(specs):
    body = ''.join(f'<FormulaField expression={quoteattr(e)} field={quoteattr(f)} size={quoteattr(str(s))} '
                   f'type={quoteattr(t)} />' for f, e, t, s in specs)
    return ref('Formula', f'<FormulaFields>{body}</FormulaFields>')


TYPE_MAP = {'Double': 'double', 'V_WString': 'v_wstring', 'Int64': 'int64', 'Bool': 'bool', 'Date': 'date',
            'Int32': 'int32'}


def formula_case(cid, desc, inputs, specs, **kw):
    fe = {'formulas': [{'field': f, 'expression': e, 'type': TYPE_MAP[t],
                        **({'size': s} if TYPE_MAP[t] == 'v_wstring' else {})} for f, e, t, s in specs]}
    case(cid, 'preparation:formula', desc, inputs, 'core.formula', fe, formula_ref(specs), {'output': 'Output'}, **kw)


formula_case('formula-arithmetic', 'arithmetic, precedence and null propagation', {'input': SALES}, [
    ('total', '[qty] * [amount]', 'Double', 8), ('calc', '1 + 2 * 3 - [qty] / 2', 'Double', 8),
    ('neg', '-[amount]', 'Double', 8)])
formula_case('formula-strings', 'string functions on text, padding, unicode and nulls', {'input': TEXT}, [
    ('up', 'Uppercase([s])', 'V_WString', 100), ('len', 'Length([s])', 'Int64', 8),
    ('l3', 'Left([s], 3)', 'V_WString', 100), ('r3', 'Right([s], 3)', 'V_WString', 100),
    ('sub', 'Substring([s], 1, 4)', 'V_WString', 100), ('trim', 'Trim([s])', 'V_WString', 100),
    ('title', 'TitleCase([s])', 'V_WString', 100), ('pad', 'PadLeft(ToString([id]), 4, "0")', 'V_WString', 100),
    ('find', 'FindString([s], "a")', 'Int64', 8), ('has', 'Contains([s], "a")', 'Bool', 1)])
formula_case('formula-null-concat', 'concatenation with a null operand', {'input': TEXT}, [
    ('cat', '[s] + "!"', 'V_WString', 100), ('isnull', 'IsNull([s])', 'Bool', 1),
    ('isempty', 'IsEmpty([s])', 'Bool', 1)])
formula_case('formula-conditionals', 'IF/ELSEIF/ELSE and IIF over nulls', {'input': PEOPLE}, [
    ('band', 'IF [score] > 80 THEN "high" ELSEIF [score] > 0 THEN "mid" ELSE "low" ENDIF', 'V_WString', 10),
    ('flag', 'IIF(IsNull([region]), "none", [region])', 'V_WString', 10)])
formula_case('formula-dates', 'date arithmetic at month ends and leap days', {'input': PEOPLE}, [
    ('plus_month', 'DateTimeAdd([joined], 1, "months")', 'Date', 10),
    ('days_to', 'DateTimeDiff("2024-12-31", [joined], "days")', 'Int64', 8),
    ('fmt', 'DateTimeFormat([joined], "%Y/%m/%d")', 'V_WString', 20),
    ('yr', 'DateTimeYear([joined])', 'Int64', 8)])
formula_case('formula-rounding', 'Round half cases, Mod with negatives, Ceil/Floor',
             {'input': {'fields': [F('x', 'double')], 'rows': [[2.5], [-2.5], [1.45], [7.0], [-7.0]]}}, [
    ('r1', 'Round([x], 1)', 'Double', 8), ('r01', 'Round([x], 0.1)', 'Double', 8),
    ('m3', 'Mod([x], 3)', 'Double', 8), ('c', 'Ceil([x])', 'Double', 8), ('f', 'Floor([x])', 'Double', 8)])
formula_case('formula-divide-by-zero', 'division by zero result',
             {'input': {'fields': [F('a', 'double'), F('b', 'double')], 'rows': [[1.0, 0.0], [0.0, 0.0], [4.0, 2.0]]}},
             [('q', '[a] / [b]', 'Double', 8)])
formula_case('formula-tonumber', 'ToNumber on clean, padded and invalid text',
             {'input': {'fields': [F('t', 'v_string', size=10)], 'rows': [['42'], [' 4.5 '], ['abc'], ['1e3'], [None]]}},
             [('n', 'ToNumber([t])', 'Double', 8)])


# --------------------------------------------------------------------------- Select
case('select-cast-rename-drop', 'preparation:select', 'retype double->Int32 (non-whole values), rename, drop',
     {'input': PEOPLE}, 'core.select',
     {'fields': [{'field': 'id'}, {'field': 'name', 'rename': 'who'}, {'field': 'region', 'selected': False},
                 {'field': 'score', 'type': 'int32'}, {'field': 'joined', 'selected': False}],
      'unknown_fields': 'exclude', 'on_conversion_error': 'null'},
     ref('AlteryxSelect', entry='AlteryxSelect', configuration='<OrderChanged value="False" /><CommaDecimal value="False" /><SelectFields>'
         '<SelectField field="id" selected="True" /><SelectField field="name" selected="True" rename="who" />'
         '<SelectField field="region" selected="False" /><SelectField field="score" selected="True" type="Int32" size="4" />'
         '<SelectField field="joined" selected="False" /><SelectField field="*Unknown" selected="False" />'
         '</SelectFields>'), {'output': 'Output'})


# --------------------------------------------------------------------------- Sort
def sort_ref(keys):
    body = ''.join(f'<Field field={quoteattr(f)} order="{"Descending" if d else "Ascending"}" />' for f, d in keys)
    return ref('Sort', f'<SortInfo locale="0">{body}</SortInfo>')


case('sort-multikey-nulls', 'preparation:sort', 'two keys, desc numeric with null, null region',
     {'input': PEOPLE}, 'core.sort', {'keys': [{'field': 'region'}, {'field': 'score', 'order': 'desc'}]},
     sort_ref([('region', False), ('score', True)]), {'output': 'Output'})
case('sort-string-case', 'preparation:sort', 'collation of mixed-case and unicode strings',
     {'input': {'fields': [F('s', 'v_wstring', size=10)],
                'rows': [['b'], ['A'], ['a'], ['B'], ['é'], ['Z'], ['_x'], ['10'], ['9']]}},
     'core.sort', {'keys': [{'field': 's'}]}, sort_ref([('s', False)]), {'output': 'Output'})


# --------------------------------------------------------------------------- Unique
case('unique-case-null', 'preparation:unique', 'case variants and nulls in the key',
     {'input': PEOPLE}, 'core.unique', {'fields': ['region']},
     ref('Unique', '<UniqueFields>' + fields_xml('Field', ['region']) + '</UniqueFields>'),
     {'unique': 'Unique', 'duplicates': 'Duplicates'})


# --------------------------------------------------------------------------- Sample / Record ID
for mode, n, ours, cid in [('First', 2, ('first', 2), 'sample-first'), ('Last', 2, ('last', 2), 'sample-last'),
                           ('Skip', 4, ('skip', 4), 'sample-skip'), ('Sample', 2, ('every_nth', 2), 'sample-every-nth'),
                           ('NPercent', 50, ('first_percent', 50), 'sample-percent')]:
    case(cid, 'preparation:sample', f'positional sample mode {mode} N={n}', {'input': PEOPLE}, 'core.sample',
         {'mode': ours[0], 'n': ours[1]},
         ref('Sample', f'<Mode>{mode}</Mode><N>{n}</N><GroupFields orderChanged="False" />'), {'output': 'Output'})
case('sample-first-grouped', 'preparation:sample', 'first N per group', {'input': PEOPLE}, 'core.sample',
     {'mode': 'first', 'n': 1, 'group_by': ['region']},
     ref('Sample', '<Mode>First</Mode><N>1</N><GroupFields orderChanged="False">'
         + fields_xml('Field', ['region']) + '</GroupFields>'), {'output': 'Output'})
case('record-id-default', 'preparation:record-id', 'Int32 ids starting at 10, first position', {'input': SALES},
     'core.record_id', {'field_name': 'RID', 'start': 10, 'type': 'int32'},
     ref('RecordID', '<FieldName>RID</FieldName><StartValue>10</StartValue><FieldType>Int32</FieldType>'
         '<FieldSize>6</FieldSize><Position>0</Position>'), {'output': 'Output'})


# --------------------------------------------------------------------------- Summarize
def summ_ref(actions):
    body = ''.join(f'<SummarizeField field={quoteattr(f)} action="{a}" rename={quoteattr(r)} />' for f, a, r in actions)
    return ref('Summarize', f'<SummarizeFields>{body}</SummarizeFields>', plugin='AlteryxSpatialPluginsGui.Summarize.Summarize',
               dll='AlteryxSpatialPluginsEngine.dll')


OPS = {'GroupBy': 'group_by', 'Sum': 'sum', 'Count': 'count', 'CountNonNull': 'count_non_null', 'Avg': 'avg',
       'Min': 'min', 'Max': 'max', 'First': 'first', 'Last': 'last', 'Concat': 'concat', 'Median': 'median',
       'StdDev': 'std_dev', 'Variance': 'variance', 'Mode': 'mode', 'CountBlank': 'count_blank',
       'CountNonBlank': 'count_non_blank', 'Longest': 'longest', 'Shortest': 'shortest'}


def summ_case(cid, desc, actions, sort_groups=False, **kw):
    fe = {'actions': [{'field': f, 'op': OPS[a], 'output': r} for f, a, r in actions]}
    case(cid, 'transform:summarize', desc, {'input': SALES}, 'core.summarize', fe, summ_ref(actions),
         {'output': 'Output'}, **kw)


summ_case('summarize-basic', 'group by with sum/count/avg/min/max over nulls', [
    ('region', 'GroupBy', 'region'), ('amount', 'Sum', 'Sum_amount'), ('qty', 'Count', 'Count'),
    ('qty', 'CountNonNull', 'CountNonNull_qty'), ('amount', 'Avg', 'Avg_amount'), ('qty', 'Min', 'Min_qty'),
    ('qty', 'Max', 'Max_qty')])
summ_case('summarize-text-and-order', 'first/last/concat and output group order', [
    ('region', 'GroupBy', 'region'), ('product', 'First', 'First_product'), ('product', 'Last', 'Last_product'),
    ('product', 'Concat', 'Concat_product')])
summ_case('summarize-statistics', 'median, sample std dev and variance', [
    ('region', 'GroupBy', 'region'), ('amount', 'Median', 'Median_amount'), ('amount', 'StdDev', 'StdDev_amount'),
    ('amount', 'Variance', 'Var_amount')], compare={'ordered': True, 'tolerance': 1e-9})
summ_case('summarize-no-groupby', 'whole-table aggregate', [
    ('amount', 'Sum', 'Sum_amount'), ('qty', 'Count', 'Count')])


# --------------------------------------------------------------------------- Join
JL = {'fields': [F('k', 'int32'), F('v', 'v_string', size=10)],
      'rows': [[1, 'a'], [2, 'b'], [2, 'b2'], [3, 'c'], [None, 'n']]}
JR = {'fields': [F('k', 'int32'), F('w', 'v_string', size=10)],
      'rows': [[2, 'x'], [2, 'y'], [4, 'z'], [None, 'q']]}


def join_ref(lk, rk, by_pos=False, select=''):
    keys = '' if by_pos else (f'<JoinInfo connection="Left">{fields_xml("Field", [lk])}</JoinInfo>'
                              f'<JoinInfo connection="Right">{fields_xml("Field", [rk])}</JoinInfo>')
    return ref('Join', '', inputs={'left': 'Left', 'right': 'Right'}) | {
        'configuration': f'<Configuration joinByRecordPos="{by_pos}">{keys}<SelectConfiguration>'
                         f'<Configuration outputConnection="Join"><OrderChanged value="False" />'
                         f'<CommaDecimal value="False" /><SelectFields>{select}'
                         f'<SelectField field="*Unknown" selected="True" /></SelectFields></Configuration>'
                         f'</SelectConfiguration></Configuration>'}


case('join-many-to-many-nulls', 'join:join', 'duplicate keys multiply; null keys never match; L/J/R outputs',
     {'left': JL, 'right': JR}, 'core.join', {'by': 'fields', 'keys': [{'left': 'k', 'right': 'k'}]},
     join_ref('k', 'k'), {'left_unjoined': 'Left', 'joined': 'Join', 'right_unjoined': 'Right'})
case('join-by-position', 'join:join', 'join by record position with unequal lengths',
     {'left': JL, 'right': JR}, 'core.join', {'by': 'position'}, join_ref('k', 'k', by_pos=True),
     {'left_unjoined': 'Left', 'joined': 'Join', 'right_unjoined': 'Right'})
case('join-string-case', 'join:join', 'string keys differing only by case',
     {'left': {'fields': [F('k', 'v_string', size=5)], 'rows': [['abc'], ['ABC'], ['x']]},
      'right': {'fields': [F('k', 'v_string', size=5), F('n', 'int32')], 'rows': [['abc', 1], ['X', 2]]}},
     'core.join', {'by': 'fields', 'keys': [{'left': 'k', 'right': 'k'}]}, join_ref('k', 'k'),
     {'left_unjoined': 'Left', 'joined': 'Join', 'right_unjoined': 'Right'})


# --------------------------------------------------------------------------- Union / Append
U1 = {'fields': [F('a', 'int32'), F('b', 'v_string', size=5)], 'rows': [[1, 'x'], [2, None]]}
U2 = {'fields': [F('b', 'v_string', size=5), F('c', 'double'), F('a', 'int32')], 'rows': [['y', 1.5, 3]]}
case('union-by-name-missing', 'join:union', 'by-name stacking with a field missing from one input',
     {'inputs': [U1, U2]}, 'core.union', {'mode': 'name', 'missing_fields': 'include'},
     ref('Union', '<ByName_ErrorMode>Ignore</ByName_ErrorMode><ByName_OutputMode>All</ByName_OutputMode>'
         '<Mode>ByName</Mode><SetOutputOrder value="False" />', inputs={'inputs': 'Input'}), {'output': 'Output'})
case('append-cartesian', 'join:append-fields', 'cartesian append with colliding names',
     {'target': {'fields': [F('a', 'int32')], 'rows': [[1], [2]]},
      'source': {'fields': [F('s', 'v_string', size=3), F('n', 'int32')], 'rows': [['p', 10], ['q', 20]]}},
     'core.append_fields', {},
     ref('AppendFields', '<CartesianMode>Allow</CartesianMode><SelectConfiguration><Configuration '
         'outputConnection="Output"><OrderChanged value="False" /><CommaDecimal value="False" /><SelectFields>'
         '<SelectField field="*Unknown" selected="True" /></SelectFields></Configuration></SelectConfiguration>',
         inputs={'target': 'Targets', 'source': 'Source'}), {'output': 'Output'})


# --------------------------------------------------------------------------- Transform
case('running-total-grouped', 'transform:running-total', 'grouped running total over nulls', {'input': SALES},
     'core.running_total', {'fields': ['qty'], 'group_by': ['region']},
     ref('RunningTotal', '<GroupByFields>' + fields_xml('Field', ['region']) + '</GroupByFields><RunningTotalFields>'
         + fields_xml('Field', ['qty']) + '</RunningTotalFields>'), {'output': 'Output'})
case('transpose-basic', 'transform:transpose', 'key + two data fields to Name/Value', {'input': SALES},
     'core.transpose', {'key_fields': ['region'], 'data_fields': ['qty', 'amount']},
     ref('Transpose', '<ErrorWarn>Ignore</ErrorWarn><KeyFields>' + fields_xml('Field', ['region'])
         + '</KeyFields><DataFields>' + fields_xml('Field', ['qty', 'amount'], selected='True')
         + '<Field field="product" selected="False" /><Field field="*Unknown" selected="False" /></DataFields>'),
     {'output': 'Output'})
case('crosstab-sum', 'transform:cross-tab', 'pivot product into columns summing qty', {'input': SALES},
     'core.cross_tab', {'group_by': ['region'], 'header_field': 'product', 'value_field': 'qty', 'method': 'sum'},
     ref('CrossTab', '<GroupFields>' + fields_xml('Field', ['region']) + '</GroupFields><HeaderField field="product" />'
         '<DataField field="qty" /><Methods><Method method="Sum" /><Separator>,</Separator>'
         '<FieldSize value="2048" /></Methods>'), {'output': 'Output'})


# --------------------------------------------------------------------------- Parse
case('text-to-columns-3', 'parse:text-to-columns', 'split on comma into 3 columns, extras kept in last',
     {'input': TEXT}, 'core.text_to_columns',
     {'field': 's', 'delimiters': ',', 'mode': 'columns', 'columns': 3, 'extra': 'last_column'},
     ref('TextToColumns', '<Field>s</Field><ErrorHandling>Last</ErrorHandling><RootName>s</RootName>'
         '<Delimeters value="," /><NumFields value="3" /><Flags value="0" />'), {'output': 'Output'})


def regex_ref(method, pattern, extra, ci=False):
    return ref('RegEx', f'<Field>s</Field><RegExExpression value={quoteattr(pattern)} />'
                        f'<CaseInsensitve value="{ci}" /><Method>{method}</Method>{extra}')


case('regex-replace', 'parse:regex', 'replace digits with #, groups via $n', {'input': TEXT}, 'core.regex',
     {'field': 's', 'pattern': r'(\d)', 'mode': 'replace', 'replacement': '<$1>'},
     regex_ref('Replace', r'(\d)', '<Replace expression="&lt;$1&gt;"><CopyUnmatched value="True" /></Replace>'),
     {'output': 'Output'})
case('regex-parse-groups', 'parse:regex', 'parse two capture groups into new fields', {'input': TEXT}, 'core.regex',
     {'field': 's', 'pattern': r'(\w+) (\w+)', 'mode': 'parse', 'group_fields': ['g1', 'g2']},
     regex_ref('ParseComplex', r'(\w+) (\w+)', '<ParseComplex><Field field="g1" type="V_WString" size="60" />'
                                              '<Field field="g2" type="V_WString" size="60" /></ParseComplex>'),
     {'output': 'Output'})
case('regex-match', 'parse:regex', 'full-match test field', {'input': TEXT}, 'core.regex',
     {'field': 's', 'pattern': r'[a-z, ]+\d*', 'mode': 'match', 'output_field': 's_Matched'},
     regex_ref('Match', r'[a-z, ]+\d*', '<Match><Field>s_Matched</Field><ErrorUnmatched value="False" /></Match>'),
     {'output': 'Output'})


# --------------------------------------------------------------------------- Multi-Row
case('multi-row-fill-down', 'preparation:multi-row-formula', 'fill nulls from the previous row (update field)',
     {'input': SALES}, 'core.multi_row_formula',
     {'field': 'qty', 'expression': 'IF IsNull([qty]) THEN [Row-1:qty] ELSE [qty] ENDIF', 'nonexistent_rows': 'null'},
     ref('MultiRowFormula', '<UpdateField value="True" /><UpdateField_Name>qty</UpdateField_Name>'
         '<CreateField_Name>NewField</CreateField_Name><CreateField_Type>Int32</CreateField_Type>'
         '<CreateField_Size>4</CreateField_Size><OtherRows>Empty</OtherRows><NumRows value="1" />'
         '<Expression>IF IsNull([qty]) THEN [Row-1:qty] ELSE [qty] ENDIF</Expression><GroupByFields />'),
     {'output': 'Output'})


# --------------------------------------------------------------------------- probes (resolve open semantics)
formula_case('probe-null-string-functions', 'are string-function results on null null or empty?', {'input': TEXT}, [
    ('n_up', 'IsNull(Uppercase([s]))', 'Bool', 1), ('n_left', 'IsNull(Left([s], 2))', 'Bool', 1),
    ('n_trim', 'IsNull(Trim([s]))', 'Bool', 1), ('n_cat', 'IsNull([s] + "x")', 'Bool', 1),
    ('n_catnull', 'IsNull([s] + [s])', 'Bool', 1), ('n_replace', 'IsNull(Replace([s], "a", "b"))', 'Bool', 1),
    ('n_tostr', 'IsNull(ToString([s]))', 'Bool', 1), ('n_len', 'IsNull(Length([s]))', 'Bool', 1)])
formula_case('probe-string-ordering', 'string < and = case sensitivity',
             {'input': {'fields': [F('a', 'v_wstring', size=5), F('b', 'v_wstring', size=5)],
                        'rows': [['a', 'B'], ['A', 'b'], ['b', 'A'], ['abc', 'ABC'], ['é', 'É']]}}, [
    ('lt', '[a] < [b]', 'Bool', 1), ('eq', '[a] = [b]', 'Bool', 1), ('ne', '[a] != [b]', 'Bool', 1),
    ('contains', 'Contains([a], [b])', 'Bool', 1)])
formula_case('probe-null-arithmetic-bool', 'null in arithmetic/boolean contexts',
             {'input': {'fields': [F('x', 'double'), F('y', 'double')], 'rows': [[None, 1.0], [None, None], [2.0, 3.0]]}}, [
    ('sum', '[x] + [y]', 'Double', 8), ('isnull_sum', 'IsNull([x] + [y])', 'Bool', 1),
    ('gt', '[x] > [y]', 'Bool', 1), ('isnull_gt', 'IsNull([x] > [y])', 'Bool', 1),
    ('eqnull', '[x] = [y]', 'Bool', 1), ('notx', '!([x] > 0)', 'Bool', 1)])
case('probe-select-int-rounding', 'preparation:select', 'double -> Int32 rounding at halves incl. negatives',
     {'input': {'fields': [F('x', 'double')], 'rows': [[2.5], [-2.5], [3.5], [-3.5], [0.49999], [-0.5], [1e12]]}},
     'core.select', {'fields': [{'field': 'x', 'type': 'int32'}], 'on_conversion_error': 'null'},
     ref('AlteryxSelect', entry='AlteryxSelect', configuration='<OrderChanged value="False" /><CommaDecimal value="False" />'
         '<SelectFields><SelectField field="x" selected="True" type="Int32" size="4" />'
         '<SelectField field="*Unknown" selected="True" /></SelectFields>'), {'output': 'Output'})
case('probe-join-unjoined-order', 'join:join', 'order of unjoined outputs and of joined rows by key',
     {'left': {'fields': [F('k', 'int32'), F('v', 'v_string', size=3)], 'rows': [[9, 'a'], [1, 'b'], [5, 'c'], [1, 'd'], [7, 'e']]},
      'right': {'fields': [F('k', 'int32'), F('w', 'v_string', size=3)], 'rows': [[8, 'p'], [5, 'q'], [1, 'r'], [2, 's'], [5, 't']]}},
     'core.join', {'by': 'fields', 'keys': [{'left': 'k', 'right': 'k'}]}, join_ref('k', 'k'),
     {'left_unjoined': 'Left', 'joined': 'Join', 'right_unjoined': 'Right'})
case('probe-unique-order', 'preparation:unique', 'order of unique/duplicate outputs (numeric key)',
     {'input': {'fields': [F('k', 'int32'), F('i', 'int32')], 'rows': [[3, 1], [1, 2], [3, 3], [2, 4], [1, 5], [None, 6]]}},
     'core.unique', {'fields': ['k']}, ref('Unique', '<UniqueFields>' + fields_xml('Field', ['k']) + '</UniqueFields>'),
     {'unique': 'Unique', 'duplicates': 'Duplicates'})
formula_case('probe-contains-flag-and-codepoints', 'third Contains argument; character codes inside the engine',
             {'input': {'fields': [F('a', 'v_wstring', size=8)], 'rows': [['abc'], ['Zoë'], ['李雷']]}}, [
    ('c0', 'Contains([a], "ABC", 0)', 'Bool', 1), ('c1', 'Contains([a], "ABC", 1)', 'Bool', 1),
    ('s0', 'StartsWith([a], "AB")', 'Bool', 1), ('cp', 'ToString(CharToInt(Substring([a], 2, 1)))', 'V_WString', 10),
    ('ln', 'Length([a])', 'Int64', 8)])
