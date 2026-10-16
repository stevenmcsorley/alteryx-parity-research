"""Independent numeric correlation matrix; no vendor report format or parity claim.

Functional source: public Pearson Correlation tool help linked in the contract.
Sample covariance uses n-1; nulls and non-finite observations are rejected explicitly.
"""
import math
import statistics

from ..errors import DataError, SchemaError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType


class PearsonCorrelation(Tool):
    manifest = ToolManifest(
        tool_id='core.pearson_correlation', version='1.0.0', display_name='Pearson Correlation',
        catalog_ids=('data-investigation:pearson-correlation',),
        inputs=(Anchor('input', 'Input'),), outputs=(Anchor('output', 'Matrix'),),
        description='Independent numeric matrix of Pearson coefficients or sample covariance. '
                    'Requires at least two finite, non-null observations per field. '
                    'Constant-field correlations are null. Vendor report layout is not implemented.',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['fields'],
            'properties': {
                'fields': {'type': 'array', 'minItems': 2, 'uniqueItems': True,
                           'items': {'type': 'string', 'minLength': 1}},
                'calculation': {'enum': ['correlation', 'covariance'], 'default': 'correlation'}}})

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        selected = cfg['fields']
        if len(table) < 2:
            raise DataError('Pearson Correlation requires at least two observations')
        columns = []
        for name in selected:
            if name not in table.schema or not table.schema.field(name).type.is_numeric:
                raise SchemaError(f'Pearson Correlation requires a numeric field: {name!r}')
            raw = table.column(name)
            if any(v is None for v in raw):
                raise DataError(f'Pearson Correlation does not accept nulls in {name!r}')
            try:
                values = [float(v) for v in raw]
            except (ValueError, OverflowError):
                raise DataError(f'Pearson Correlation requires finite values in {name!r}') from None
            if not all(math.isfinite(v) for v in values):
                raise DataError(f'Pearson Correlation requires finite values in {name!r}')
            columns.append(values)
        result = []
        covariance = cfg['calculation'] == 'covariance'
        for i, left in enumerate(columns):
            for j, right in enumerate(columns):
                if not covariance and (min(left) == max(left) or min(right) == max(right)):
                    value = None
                else:
                    try:
                        value = (statistics.covariance(left, right) if covariance
                                 else statistics.correlation(left, right))
                    except (OverflowError, statistics.StatisticsError):
                        raise DataError('Correlation calculation exceeded supported numeric range') from None
                    if not math.isfinite(value):
                        raise DataError('Correlation calculation exceeded supported numeric range')
                    if not covariance:
                        value = max(-1.0, min(1.0, value))
                result.append((selected[i], selected[j], value))
        schema = [Field('Field1', FieldType.V_WSTRING, max(map(len, selected))),
                  Field('Field2', FieldType.V_WSTRING, max(map(len, selected))),
                  Field('Covariance' if covariance else 'Correlation', FieldType.DOUBLE)]
        return {'output': Table.from_rows(schema, result)}


TOOLS = [PearsonCorrelation]
