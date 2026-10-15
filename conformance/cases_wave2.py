"""Wave-2 conformance cases (Milestone 2): original inputs; reference configuration XML authored from
observed element/question names (data/reference-observations.json and bundled macro structure)."""
from xml.sax.saxutils import escape, quoteattr

from cases import F, PEOPLE, TEXT, formula_case, ref

CASES_WAVE2 = []


def case(case_id, catalog_id, description, inputs, fe_tool, fe_config, reference, outputs, **kw):
    CASES_WAVE2.append({'case_id': case_id, 'catalog_id': catalog_id, 'description': description, 'inputs': inputs,
                        'flowengine': {'tool': fe_tool, 'config': fe_config}, 'reference': reference,
                        'outputs': outputs, **kw})


def macro_ref(macro, values: dict, inputs=None):
    body = ''.join(f'<Value name={quoteattr(k)}>{escape(str(v))}</Value>' for k, v in values.items())
    return {'macro': macro, 'configuration': f'<Configuration>{body}</Configuration>',
            'input_anchors': inputs or {'input': 'Input'}}


W2 = {'fields': [F('g', 'v_string', size=5), F('x', 'double'), F('w', 'double')],
      'rows': [['a', 1.0, 1.0], ['a', 3.0, 3.0], ['b', None, 2.0], ['b', 10.0, 0.0], ['a', 3.0, 1.0], ['c', 7.0, 2.0]]}

# Determine the reference engine's mixed FixedDecimal/Double comparison rule;
# this is intentionally differential because Python coercion is not a parity oracle.
formula_case('probe-fixed-decimal-double-comparison',
     'FixedDecimal compared with a Double literal at binary precision boundaries',
     {'input': {'fields': [F('amount', 'fixed_decimal', size=25, scale=2)],
                'rows': [['9007199254740993.00'], ['9007199254740993.01'],
                         ['9007199254740992.00'], ['0.10']]}},
     [('above', '[amount] > 9007199254740992.0', 'Bool', 1),
      ('tenth_equal', '[amount] = 0.1', 'Bool', 1)])

case('autofield-types', 'preparation:auto-field', 'smallest type for numeric/date/text columns',
     {'input': {'fields': [F('i', 'v_string', size=10), F('big', 'v_string', size=10), F('d', 'v_string', size=10),
                           F('dt', 'v_string', size=12), F('s', 'v_wstring', size=10)],
                'rows': [['1', '70000', '1.5', '2024-01-02', 'ab'], ['255', '-3', '2', '2024-02-29', 'xyz'],
                         ['7', '5', '0.25', '2023-12-31', 'q']]}},
     'core.auto_field', {}, ref('AutoField', '<Fields><Field field="*Unknown" selected="True" /></Fields>'),
     {'output': 'Output'})

case('multi-field-formula-upper', 'preparation:multi-field-formula', 'expression over a text field incl. null',
     {'input': TEXT}, 'core.multi_field_formula',
     {'fields': ['s'], 'expression': 'Uppercase([_CurrentField_]) + "!"'},
     ref('MultiFieldFormula', '<FieldType>Text</FieldType><Fields orderChanged="False"><Field name="s" /></Fields>'
         '<CopyOutput value="False" /><Expression>Uppercase([_CurrentField_]) + "!"</Expression>'
         '<ChangeFieldType value="False" />'), {'output': 'Output'})

case('tile-equal-records', 'preparation:tile', 'equal-record tiles: 6 records into 4 tiles', {'input': W2},
     'core.tile', {'method': 'equal_records', 'tiles': 4},
     ref('Tile', '<GroupFields orderChanged="False" /><Method>EqualRecords</Method><EqualRecords><NumTiles value="4" />'
         '</EqualRecords>'), {'output': 'Output'})

case('find-replace-any', 'join:find-replace', 'replace any-part matches, case-insensitive',
     {'find': {'fields': [F('txt', 'v_wstring', size=30)], 'rows': [['the cat sat'], ['Dog days'], [None], ['none']]},
      'replace': {'fields': [F('find', 'v_string', size=10), F('rep', 'v_string', size=10)],
                  'rows': [['cat', 'lion'], ['dog', 'wolf']]}},
     'core.find_replace', {'find_field': 'txt', 'search_field': 'find', 'mode': 'replace', 'replace_field': 'rep',
                           'case_insensitive': True},
     ref('FindReplace', '<FieldFind>txt</FieldFind><FieldSearch>find</FieldSearch><ReplaceFoundField>rep</ReplaceFoundField>'
         '<FindMode>FindAny</FindMode><NoCase value="True" /><MatchWholeWord value="False" /><ReplaceMode>Replace</ReplaceMode>'
         '<ReplaceMultipleFound value="True" /><ReplaceAppendFields />', inputs={'find': 'Targets', 'replace': 'Source'}),
     {'output': 'Output'})

case('make-group-components', 'join:make-group', 'connected components of key pairs',
     {'input': {'fields': [F('a', 'v_string', size=5), F('b', 'v_string', size=5)],
                'rows': [['k3', 'k1'], ['k2', 'k5'], ['k1', 'k4'], ['k9', 'k9']]}},
     'core.make_group', {'key1': 'a', 'key2': 'b'}, ref('MakeGroup', '<Key1st>a</Key1st><Key2nd>b</Key2nd>'),
     {'output': 'Output'})

case('datetime-month-name', 'parse:datetime', 'parse "Month dd, yyyy" text incl. invalid text',
     {'input': {'fields': [F('s', 'v_string', size=30)], 'rows': [['March 05, 2024'], ['nope'], ['December 31, 1999']]}},
     'core.datetime', {'direction': 'to_date', 'field': 's', 'format': 'Month dd, yyyy', 'output_field': 'd'},
     ref('DateTime', '<IsFrom value="False" /><InputFieldName>s</InputFieldName><Language>English</Language>'
         '<Format>Month dd, yyyy</Format><OutputFieldName>d</OutputFieldName>'), {'output': 'Output'})

case('xml-parse-children', 'parse:xml-parse', 'child values of repeated elements',
     {'input': {'fields': [F('x', 'v_wstring', size=300)],
                'rows': [['<orders><order><sku>A</sku><qty>2</qty></order><order><sku>B</sku><qty>5</qty></order></orders>']]}},
     'core.xml_parse', {'field': 'x', 'element': 'order', 'attributes': False},
     ref('XMLParse', '<XMLField>x</XMLField><XMLElement>order</XMLElement><ChildValues value="True" />'
         '<OuterXML value="False" /><ParseRoot value="False" /><IgnoreErrors value="False" />'), {'output': 'Output'})

case('make-columns-horizontal', 'transform:make-columns', 'five records into two columns',
     {'input': {'fields': [F('v', 'int32')], 'rows': [[1], [2], [3], [4], [5]]}},
     'core.make_columns', {'columns': 2},
     ref('MakeColumns', '<NumColumns>2</NumColumns><IsHorizontal value="True" /><GroupFields orderChanged="False" />'),
     {'output': 'Output'})

case('field-info-basic', 'developer:field-info', 'schema description records', {'input': PEOPLE},
     'core.field_info', {}, ref('FieldInfo', ''), {'output': 'Output'},
     # Source names the upstream tool chain, which differs by construction between the two sides of the harness
     # (the reference side has extra Formula/Select nodes); every other column is compared exactly.
     compare={'ordered': True, 'ignore_fields': ['Source'],
              'ignore_reason': 'Source reflects harness-specific upstream tools'})

case('json-parse-single', 'developer:json-parse', 'flatten nested JSON to name/value',
     {'input': {'fields': [F('j', 'v_wstring', size=200)], 'rows': [['{"a": 1, "b": [true, {"c": "x"}], "d": 2.5}']]}},
     'core.json_parse', {'field': 'j'},
     ref('JSONParse', '<JSON_Field>j</JSON_Field><IncludeInOutput value="False" /><SingleField value="True" />'),
     {'output': 'Output'})

case('dynamic-rename-first-row', 'developer:dynamic-rename', 'take field names from the first row',
     {'input': {'fields': [F('Field1', 'v_string', size=10), F('Field2', 'v_string', size=10)],
                'rows': [['name', 'city'], ['Ada', 'Leeds'], ['Ben', 'York']]}},
     'core.dynamic_rename', {'mode': 'first_row'},
     ref('DynamicRename', '<RenameMode>FirstRow</RenameMode><Fields orderChanged="False"><Field name="Field1" />'
         '<Field name="Field2" /><Field name="*Unknown" /></Fields><Expression /><FirstRow><OnError>Warn</OnError>'
         '</FirstRow>', inputs={'input': 'Targets'}), {'output': 'Output'})

case('dynamic-select-types', 'developer:dynamic-select', 'keep double fields', {'input': W2},
     'core.dynamic_select', {'mode': 'types', 'types': ['double']},
     ref('DynamicSelect', '<Mode>FieldTypes</Mode><FieldTypes>Double</FieldTypes>'), {'output': 'Output'})

case('count-records-macro', 'transform:count-records', 'count via the bundled macro', {'input': W2},
     'core.count_records', {}, macro_ref('CountRecords.yxmc', {}, inputs={'input': 'Input8'}), {'output': 'Output9'})

case('weighted-average-macro', 'transform:weighted-average', 'weighted average without groups', {'input': W2},
     'core.weighted_average', {'value': 'x', 'weight': 'w'},
     macro_ref('WeightedAvg.yxmc', {'Value': 'x', 'Weight': 'w', 'OutputFieldName': 'WeightedAverage', 'GroupFields': ''}),
     {'output': 'Output'}, compare={'ordered': True, 'tolerance': 1e-12})

case('select-records-macro', 'preparation:select-records', 'record ranges',
     {'input': {'fields': [F('i', 'int32')], 'rows': [[i] for i in range(1, 16)]}},
     'core.select_records', {'ranges': '-2\n5-6\n9\n14+'},
     macro_ref('SelectRecords.yxmc', {'Ranges': '-2\n5-6\n9\n14+'}), {'output': 'Output'})

case('imputation-mean-macro', 'preparation:imputation', 'replace null with the mean', {'input': W2},
     'core.imputation', {'fields': ['x'], 'with': 'mean'},
     macro_ref('Imputation_v3.yxmc', {'listbox Select Incoming Fields': '"x"', 'radio Null Value': 'True',
                                      'radio User Specified Replace From Value': 'False', 'updown User Replace Value': '0',
                                      'radio Mean': 'True', 'radio Median': 'False', 'radio Mode': 'False',
                                      'radio User Specified Replace With Value': 'False',
                                      'updown User Replace With Value': '0', 'checkbox Impute Indicator': 'False',
                                      'checkbox Imputed Values Separate Field': 'False'}),
     {'output': 'Output'})

# probes: DateTime format vocabulary and JSON false rendering
for i, fmt in enumerate(['yyyy-MM-dd', 'dd/MM/yyyy', 'MMMM dd, yyyy', 'MMM d, yyyy', 'Month dd, yyyy', 'Month d, yyyy']):
    sample = {'yyyy-MM-dd': '2024-03-05', 'dd/MM/yyyy': '05/03/2024', 'MMMM dd, yyyy': 'March 05, 2024',
              'MMM d, yyyy': 'Mar 5, 2024', 'Month dd, yyyy': 'March 05, 2024', 'Month d, yyyy': 'March 5, 2024'}[fmt]
    case(f'probe-datetime-format-{i}', 'parse:datetime', f'probe format {fmt!r}',
         {'input': {'fields': [F('s', 'v_string', size=30)], 'rows': [[sample]]}},
         'core.datetime', {'direction': 'to_date', 'field': 's', 'format': fmt, 'output_field': 'd'},
         ref('DateTime', f'<IsFrom value="False" /><InputFieldName>s</InputFieldName><Language>English</Language>'
             f'<Format>{escape(fmt)}</Format><OutputFieldName>d</OutputFieldName>'), {'output': 'Output'})
case('probe-json-false-null', 'developer:json-parse', 'false and null leaves',
     {'input': {'fields': [F('j', 'v_wstring', size=100)], 'rows': [['{"f": false, "n": null, "e": [], "s": "t"}']]}},
     'core.json_parse', {'field': 'j'},
     ref('JSONParse', '<JSON_Field>j</JSON_Field><IncludeInOutput value="False" /><SingleField value="True" />'),
     {'output': 'Output'})
case('datetime-to-text', 'parse:datetime', 'date to text with a custom pattern',
     {'input': {'fields': [F('d', 'date')], 'rows': [['2024-03-05'], [None], ['1999-12-31']]}},
     'core.datetime', {'direction': 'to_text', 'field': 'd', 'format': 'dd/MM/yyyy', 'output_field': 's'},
     ref('DateTime', '<IsFrom value="True" /><InputFieldName>d</InputFieldName><Language>English</Language>'
         '<Format>dd/MM/yyyy</Format><OutputFieldName>s</OutputFieldName>'), {'output': 'Output'})


def _sort_ref(field, locale):
    return ref('Sort', f'<SortInfo locale="{locale}"><Field field="{field}" order="Ascending" /></SortInfo>')


COLLATE = {'input': {'fields': [F('s', 'v_wstring', size=12)],
                     'rows': [['b10'], ['b2'], ['B1'], ['a'], ['A'], ['10'], ['9'], ['é'], ['e'], ['Z'], ['_x'], ['22'],
                              ['4'], ['a-b'], ['ab'], ['a b'], ['b1'], ['é1'], ['E']]}}
case('probe-sort-locale-1033', 'preparation:sort', 'collation with locale 1033 (English)', COLLATE,
     'core.sort', {'keys': [{'field': 's'}], 'collation': 'locale'}, _sort_ref('s', 1033), {'output': 'Output'})

CT = {'input': {'fields': [F('g', 'v_string', size=5), F('h', 'v_string', size=20), F('v', 'double')],
                'rows': [['a', 'Fun and Games', 1.0], ['a', 'General', 2.0], ['b', 'Fun and Games', 4.0],
                         ['a', 'Fun and Games', 3.0], ['b', 'x-y.z', 5.0]]}}
case('probe-crosstab-space-headers', 'transform:cross-tab', 'header values with spaces and punctuation, one method',
     CT, 'core.cross_tab', {'group_by': ['g'], 'header_field': 'h', 'value_field': 'v', 'method': 'sum'},
     ref('CrossTab', '<GroupFields><Field field="g" /></GroupFields><HeaderField field="h" /><DataField field="v" />'
         '<Methods><Method method="Sum" /><Separator>,</Separator><FieldSize value="2048" /></Methods>'),
     {'output': 'Output'})
case('crosstab-two-methods', 'transform:cross-tab', 'sum and average in one cross tab',
     CT, 'core.cross_tab', {'group_by': ['g'], 'header_field': 'h', 'value_field': 'v', 'methods': ['sum', 'avg']},
     ref('CrossTab', '<GroupFields><Field field="g" /></GroupFields><HeaderField field="h" /><DataField field="v" />'
         '<Methods><Method method="Sum" /><Method method="Avg" /><Separator>,</Separator><FieldSize value="2048" />'
         '</Methods>'), {'output': 'Output'})
