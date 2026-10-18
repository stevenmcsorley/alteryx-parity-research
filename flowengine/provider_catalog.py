"""Built-in providers. Each entry says what it serves, what it needs, and what has been verified."""
from .providers import Provider, register

register(Provider(
    id='r', family='r', name='R (Rscript)', kind='local-runtime', serves=('developer:r',), licence='GPL-2.0-or-later',
    needs=('rscript',), homepage='https://www.r-project.org',
    semantics={'exchange': 'CSV; output fields are text'},
    setup='Install R from CRAN, or set FLOWENGINE_RSCRIPT to the Rscript executable.',
    verification='Runtime-gated test runs in the r-runtime CI job against R 4.6.0.'))

register(Provider(
    id='spark-local', family='spark', name='Apache Spark (local mode)', kind='local-runtime',
    serves=('developer:apache-spark-code',), licence='Apache-2.0', needs=('python:pyspark', 'exe:java'),
    homepage='https://spark.apache.org',
    semantics={'execution': 'Spark local mode in one machine', 'languages': ['python'], 'timezone': 'UTC',
               'types': 'Flowbench types mapped to Spark SQL types and back'},
    setup='pip install pyspark into the Python environment the tool uses, and install a Java 17+ runtime.',
    verification='Runtime-gated test runs in the spark-runtime CI job (PySpark with Java 17). '
                 'Remote clusters (Livy, Databricks) are not served by this provider.'))
