"""Stage a relocatable uv-managed CPython for the current Linux/macOS architecture.

Requires uv on PATH. Run on each native build host; never copy a system Python or venv.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def link_wheel_libraries(runtime: Path):
    """Let libraries vendored by manylinux wheels find each other.

    auditwheel sets RPATH on the extension modules only. The vendored ``*.libs``
    files depend on one another without a search path, which linuxdeploy (AppImage)
    cannot resolve. ``$ORIGIN`` resolves them in place; libraries that already
    declare a search path are left unchanged.
    """
    for library in sorted(runtime.glob('lib/python3.*/site-packages/*.libs/*.so*')):
        if library.is_file() and not library.is_symlink():
            current = subprocess.check_output(['patchelf', '--print-rpath', str(library)], text=True).strip()
            if not current:
                subprocess.run(['patchelf', '--set-rpath', '$ORIGIN', str(library)], check=True)


def stage():
    if sys.platform not in ('linux', 'darwin'):
        raise SystemExit('Build Linux/macOS runtimes on their native operating system')
    runtime = ROOT / 'desktop/src-tauri/runtime'
    if any(p.name != '.gitkeep' for p in runtime.iterdir()):
        raise SystemExit('Runtime staging directory must be empty; use a clean checkout')
    with tempfile.TemporaryDirectory(prefix='flowbench-runtime-') as temporary:
        env = dict(os.environ, UV_PYTHON_INSTALL_DIR=temporary)
        subprocess.run(['uv', 'python', 'install', '3.12.10', '--no-bin'], env=env, check=True)
        python = Path(subprocess.check_output(
            ['uv', 'python', 'find', '--managed-python', '3.12.10'], env=env, text=True).strip())
        installation = python.parent.parent
        shutil.copytree(installation, runtime, dirs_exist_ok=True, symlinks=True)
    python = runtime / 'bin/python3'
    subprocess.run(['uv', 'pip', 'install', '--break-system-packages', '--python', str(python),
                    '-r', str(ROOT / 'requirements-unix-runtime.txt')], check=True)
    if sys.platform == 'linux':
        link_wheel_libraries(runtime)
    # Test after relocation, from outside the repo, to catch absolute runtime links.
    with tempfile.TemporaryDirectory(prefix='flowbench-relocated-') as relocated:
        moved = Path(relocated) / 'runtime'
        shutil.copytree(runtime, moved, symlinks=True)
        subprocess.run([str(moved / 'bin/python3'), '-c',
            'import jsonschema, pyarrow, psycopg, pymssql, scipy.stats, reportlab.platypus, PIL.Image, shapely, pyproj, sklearn.ensemble, statsmodels.api, skops.io, networkx, yxdb, keyring, vaderSentiment.vaderSentiment, pypdfium2, zxingcpp, openpyxl, venv; '
            'from metaphone import doublemetaphone; assert doublemetaphone("Smith")[0]'],
            cwd=relocated, check=True)
        subprocess.run([str(moved / 'bin/python3'), '-m', 'flowengine', 'run',
                        'examples/sales/monthly_sales.flow.json', '--clock', '2026-01-15T09:30:00'],
                       cwd=ROOT, check=True)


if __name__ == '__main__':
    stage()
