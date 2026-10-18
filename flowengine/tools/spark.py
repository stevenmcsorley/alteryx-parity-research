"""Apache Spark Code: run PySpark code over the workflow's tables.

Provider ``spark-local`` runs Spark in local mode (``local[*]`` or a ``local[n]`` master) inside a
child Python process that has ``pyspark`` and a Java runtime. Inputs reach the code as Spark
DataFrames with types mapped from Flowbench types; DataFrames written back are collected and
mapped back, so output types are exact rather than inferred. Remote clusters are a separate
provider and are not served here.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from ..errors import ConfigError
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from .developer import _EXCHANGE_HELPER, _read_exchange, _require_processes, _run_process, _write_exchange

_SPARK_HELPER = r'''
"""flowbench_spark: Spark DataFrames in and out of Flowbench (used by the Apache Spark Code tool)."""
import datetime, decimal, json, os
import flowbench_io
from pyspark.sql import SparkSession, types as T

spark = (SparkSession.builder.master(os.environ['FLOWBENCH_SPARK_MASTER']).appName('flowbench')
         .config('spark.ui.enabled', 'false').config('spark.sql.session.timeZone', 'UTC').getOrCreate())
spark.sparkContext.setLogLevel('ERROR')

_TO_SPARK = {'bool': T.BooleanType(), 'byte': T.ShortType(), 'int16': T.ShortType(), 'int32': T.IntegerType(),
             'int64': T.LongType(), 'float': T.FloatType(), 'double': T.DoubleType(), 'date': T.DateType(),
             'datetime': T.TimestampNTZType(), 'time': T.StringType(), 'blob': T.BinaryType()}

def _spark_type(f):
    if f['type'] == 'fixed_decimal':
        return T.DecimalType(min(int(f.get('size') or 38), 38), int(f.get('scale') or 0))
    return _TO_SPARK.get(f['type'], T.StringType())

def read_input(n=1):
    """Input connection #n as a Spark DataFrame."""
    with open(os.path.join(flowbench_io._DIR, f'in{n}.jsonl'), encoding='utf-8') as f:
        schema = json.loads(f.readline())
    rows = [tuple(r[x['name']] for x in schema) for r in flowbench_io.read_input(n)]
    rows = [tuple(v.isoformat() if isinstance(v, datetime.time) else v for v in r) for r in rows]
    return spark.createDataFrame(rows, T.StructType([T.StructField(x['name'], _spark_type(x), True) for x in schema]))

def _flowbench_field(f):
    t = f.dataType
    if isinstance(t, T.DecimalType):
        return {'name': f.name, 'type': 'fixed_decimal', 'size': t.precision, 'scale': t.scale}
    simple = [(T.BooleanType, 'bool'), (T.ByteType, 'byte'), (T.ShortType, 'int16'), (T.IntegerType, 'int32'),
              (T.LongType, 'int64'), (T.FloatType, 'float'), (T.DoubleType, 'double'), (T.DateType, 'date'),
              (T.TimestampNTZType, 'datetime'), (T.TimestampType, 'datetime'), (T.BinaryType, 'blob')]
    for cls, name in simple:
        if isinstance(t, cls):
            return {'name': f.name, 'type': name}
    if isinstance(t, T.StringType):
        return {'name': f.name, 'type': 'v_wstring', 'size': 1073741823}
    raise TypeError(f'Field {f.name!r} has Spark type {t.simpleString()}, which Flowbench cannot hold; '
                    'convert it (for example with to_json or cast to string) before write_output')

def write_output(n, df):
    """Collect DataFrame df as output #n (1-5)."""
    schema = [_flowbench_field(f) for f in df.schema.fields]
    names = [f['name'] for f in schema]
    if len(set(names)) != len(names):
        raise ValueError('write_output needs unique column names')
    rows = []
    for r in df.toLocalIterator():
        v = r.asDict()
        rows.append({k: (v[k].replace(tzinfo=None) if isinstance(v[k], datetime.datetime) else v[k]) for k in names})
    flowbench_io.write_output(n, rows, schema)
'''


class SparkCode(Tool):
    manifest = ToolManifest(
        tool_id='core.spark_code', version='1.0.0', display_name='Apache Spark Code',
        catalog_ids=('developer:apache-spark-code',),
        inputs=(Anchor('inputs', 'I', required=False, multiple=True, description='Read with read_input(n)'),),
        outputs=tuple(Anchor(f'output{i}', str(i)) for i in range(1, 6)),
        side_effects='process', deterministic=False,
        description='Run PySpark code. "from flowbench_spark import spark, read_input, write_output": read_input(n) '
                    'is a Spark DataFrame of input n, write_output(n, df) collects df as output n. Provider spark-local '
                    'runs Spark in local mode (needs pyspark and Java in the chosen Python environment).',
        config_schema={'type': 'object', 'additionalProperties': False, 'required': ['code', 'provider'], 'properties': {
            'provider': {'type': 'string', 'enum': ['spark-local']},
            'provider_semantics': {'type': 'string'},
            'code': {'type': 'string', 'minLength': 1},
            'master': {'type': 'string', 'default': 'local[*]'},
            'environment': {'type': 'string', 'default': 'default'},
            'timeout_seconds': {'type': 'integer', 'minimum': 1, 'default': 900}}})

    def infer(self, ctx, cfg, inputs):
        return None

    def run(self, ctx, cfg, inputs):
        from .. import providers
        providers.resolve(cfg, 'spark')
        _require_processes(ctx, 'The Apache Spark Code tool')
        if not re.fullmatch(r'local(\[(\*|[1-9][0-9]{0,3})\])?', cfg['master']):
            raise ConfigError('The spark-local provider runs Spark in local mode: master must be local, local[*] or local[n]')
        from ..runtimes import clean_env, interpreter
        exe = interpreter(cfg['environment'])
        with tempfile.TemporaryDirectory(prefix='flowbench-spark-') as d:
            d = Path(d)
            (d / 'flowbench_io.py').write_text(_EXCHANGE_HELPER, encoding='utf-8')
            (d / 'flowbench_spark.py').write_text(_SPARK_HELPER, encoding='utf-8')
            for k, t in enumerate(inputs.get('inputs') or []):
                _write_exchange(d / f'in{k + 1}.jsonl', t)
            (d / 'user_code.py').write_text(cfg['code'] + '\n\nimport flowbench_spark as _fb\n_fb.spark.stop()\n', encoding='utf-8')
            env = clean_env({'FLOWBENCH_SPARK_MASTER': cfg['master'], 'PYSPARK_PYTHON': exe,
                             **({'JAVA_HOME': os.environ['JAVA_HOME']} if os.environ.get('JAVA_HOME') else {})})
            _run_process(ctx, [exe, '-I', '-X', 'utf8', '-c',
                               f'import sys; sys.path.insert(0, {str(d)!r}); '
                               f'exec(compile(open({str(d / "user_code.py")!r}, encoding="utf-8").read(), '
                               f'"user_code.py", "exec"), {{"__name__": "__main__"}})'],
                         d, cfg['timeout_seconds'], env=env)
            return {f'output{i}': (_read_exchange(d / f'out{i}.jsonl', ctx) if (d / f'out{i}.jsonl').is_file()
                                   else Table([], [])) for i in range(1, 6)}


TOOLS = [SparkCode]
