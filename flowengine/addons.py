"""Optional components that download on first use: the language pack (spaCy), the neural runtime (ONNX
Runtime) and their models.

They are not bundled in the installers. A workflow that needs one lists it in its preflight
(``workflow_effects(...)['downloads']``). The desktop app asks once, then calls :func:`install`; the CLI
uses ``python -m flowengine addons install``. A run never downloads anything itself: a missing component
stops the tool with ``addon-missing`` and says how to install it.

* **Libraries** go into one Flowbench-managed virtual environment, ``<home>/envs/flowbench-nlp``, built
  from the engine's own interpreter. pip installs binary wheels only, with every version fixed by
  ``CONSTRAINTS``.
* **spaCy models** are the official wheels from github.com/explosion/spacy-models. They are checked
  against their SHA-256 before being installed into that environment.
* **ONNX models** are files from huggingface.co at a pinned revision, each checked against its SHA-256 and
  stored under ``<home>/models``.

Tools run these libraries in a separate process, using the scripts in ``flowengine/addon_workers``, so
the engine itself never imports them. ``size_mb`` is the approximate download size. Installed, the
environment takes about 440 MB of disk and the zero-shot model about 92 MB.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from .errors import ConfigError, DataError, EngineError
from .runtimes import env_dir, home

ENV = 'flowbench-nlp'
CONSTRAINTS = """annotated-types==0.8.0
blis==1.3.3
catalogue==2.0.10
certifi==2026.7.22
charset-normalizer==3.5.2
click==8.5.0
cloudpathlib==0.26.0
colorama==0.4.6
confection==1.3.3
cymem==2.0.13
flatbuffers==25.12.19
idna==3.20
Jinja2==3.1.6
markdown-it-py==4.2.0
MarkupSafe==3.0.4
mdurl==0.1.2
murmurhash==1.0.15
numpy==2.5.4
onnxruntime==1.31.0
packaging==26.3
preshed==3.0.13
protobuf==7.36.2
pydantic==2.14.1
pydantic_core==2.50.1
Pygments==2.21.0
PyYAML==6.0.3
requests==2.34.2
rich==15.0.0
setuptools==84.0.0
shellingham==1.5.4
smart_open==8.0.3
spacy==3.8.16
spacy-legacy==3.0.12
spacy-loggers==1.0.5
srsly==2.5.4
thinc==8.3.13
tokenizers==0.23.3
tqdm==4.70.1
typer==0.27.3
typing-inspection==0.4.4
typing_extensions==4.16.0
urllib3==2.8.0
wasabi==1.1.3
weasel==1.0.0
wrapt==2.5.1
"""
SPACY_MODELS = {
    'english': ('en_core_web_sm', 12806118, '1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85'),
    'french': ('fr_core_news_sm', 16271721, '7d6ad14cd5078e53147bfbf70fb9d433c6a3865b695fda2657140bbc59a27e29'),
    'german': ('de_core_news_sm', 14639490, 'fec69fec52b1780f2d269d5af7582a5e28028738bd3190532459aeb473bfa3e7'),
    'italian': ('it_core_news_sm', 13030943, '3f617bf9a8ae0418953cf1fbf014e10272684c4229e882a7fd748b637d0100bf'),
    'portuguese': ('pt_core_news_sm', 12985007, 'c304fa04db3af73cd08a250feacf560506e15a2ec2469bd1b09f06847f6b455c'),
    'spanish': ('es_core_news_sm', 12884212, 'e451a83d6df79b87e9eed0cb553f03e99e36a3bab18a7b79f0dcfd1fdf875e12'),
}
SPACY_MODEL_VERSION = '3.8.0'
ZERO_SHOT = {
    'repo': 'Xenova/nli-deberta-v3-xsmall', 'revision': '2a4f614a701367a02d51389039afc998faeda637',
    'source': 'cross-encoder/nli-deberta-v3-xsmall (Apache-2.0), quantized ONNX export',
    'files': {
        'config.json': ('config.json', 1038, 'ec0bd14cc28640326474399cd61d38ccd52b64900228799d0f81debda8c4bc53'),
        'tokenizer.json': ('tokenizer.json', 8656551, 'a86f883318afa11c8c10466f1bf4efaeb6ded28a52cbe57217a8fa0d0a2a87df'),
        'model.onnx': ('onnx/model_quantized.onnx', 87246587, '3fac2500c45c75af42c7711de0d1b93d59577456100208be0dc1f9e8811946b6'),
    },
}


def _components():
    out = {
        'language-pack': {'title': 'Flowbench language pack (spaCy 3.8)', 'size_mb': 75, 'source': 'pypi.org',
                          'requires': []},
        'neural-runtime': {'title': 'Neural model runtime (ONNX Runtime 1.31)', 'size_mb': 45, 'source': 'pypi.org',
                           'requires': []},
        'zero-shot-model': {'title': 'Zero-shot classification model (DeBERTa-v3 xsmall NLI)', 'size_mb': 92,
                            'source': 'huggingface.co', 'requires': ['neural-runtime']},
    }
    for lang, (name, size, _) in SPACY_MODELS.items():
        out[f'spacy-model:{lang}'] = {'title': f'{lang.title()} language model ({name} {SPACY_MODEL_VERSION})',
                                      'size_mb': round(size / 1e6), 'source': 'github.com/explosion',
                                      'requires': ['language-pack']}
    return out


COMPONENTS = _components()


def _marker(component: str) -> Path:
    return home() / 'addons' / f'{component.replace(":", "-")}.json'


def env_python() -> Path:
    d = env_dir(ENV)
    return d / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def installed(component: str) -> bool:
    if component not in COMPONENTS:
        raise ConfigError(f'Unknown component {component!r}')
    if not _marker(component).is_file():
        return False
    if component == 'zero-shot-model':
        return all((zero_shot_dir() / name).is_file() for name in ZERO_SHOT['files'])
    return env_python().is_file()


def with_requirements(components) -> list[str]:
    """Components plus everything they require, dependencies first."""
    order = []

    def add(c):
        for r in COMPONENTS[c]['requires']:
            add(r)
        if c not in order:
            order.append(c)
    for c in components:
        if c not in COMPONENTS:
            raise ConfigError(f'Unknown component {c!r}')
        add(c)
    return order


def missing(components) -> list[dict]:
    return [{'id': c, **COMPONENTS[c]} for c in with_requirements(components) if not installed(c)]


def require(components, tool: str):
    """Raise ``addon-missing`` if any component is absent."""
    absent = missing(components)
    if absent:
        names = ', '.join(c['title'] for c in absent)
        raise EngineError(f'{tool} needs {names}, which download on first use. Run the workflow from the desktop app '
                          f'and accept the download, or run: python -m flowengine addons install '
                          f'{" ".join(c["id"] for c in absent)}', code='addon-missing')


def zero_shot_dir() -> Path:
    return home() / 'models' / 'nli-deberta-v3-xsmall' / ZERO_SHOT['revision']


# =========================================================================== installation

def _download(url: str, target: Path, size: int, sha256: str, progress=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    tmp = target.with_suffix(target.suffix + '.part')
    req = urllib.request.Request(url, headers={'User-Agent': 'Flowbench'})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, 'wb') as out:
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            done += len(chunk)
            if done > size * 1.01 + 1024:
                raise DataError(f'Download of {target.name} is larger than expected; stopped')
            digest.update(chunk)
            out.write(chunk)
            if progress:
                progress(done, size)
    if digest.hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        raise DataError(f'Download of {target.name} failed its checksum; nothing was installed')
    tmp.replace(target)


def _pip(args, log):
    exe = str(env_python())
    p = subprocess.run([exe, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                        '--only-binary=:all:', *args], capture_output=True, text=True)
    log(p.stdout[-2000:])
    if p.returncode != 0:
        raise DataError(f'Installing Python packages failed: {(p.stderr or p.stdout).strip()[-1500:]}', code='addon-install-failed')


def _ensure_env(log):
    if env_python().is_file():
        return
    d = env_dir(ENV)
    d.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([sys.executable, '-m', 'venv', str(d)], capture_output=True, text=True)
    if p.returncode != 0:
        shutil.rmtree(d, ignore_errors=True)
        raise DataError(f'Could not create the add-on environment: {p.stderr.strip()[-1000:]}', code='addon-install-failed')


def _constraints_file() -> Path:
    path = home() / 'addons' / 'constraints.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONSTRAINTS, encoding='utf-8')
    return path


def install(components, log=lambda m: None) -> list[str]:
    """Install the components (with their requirements) that are missing; returns what was installed."""
    done = []
    for c in with_requirements(components):
        if installed(c):
            continue
        log(f'Installing {COMPONENTS[c]["title"]}')
        if c == 'language-pack':
            _ensure_env(log)
            _pip(['-c', str(_constraints_file()), 'spacy==3.8.16'], log)
        elif c == 'neural-runtime':
            _ensure_env(log)
            _pip(['-c', str(_constraints_file()), 'onnxruntime==1.31.0'], log)
            _pip(['-c', str(_constraints_file()), '--no-deps', 'tokenizers==0.23.3'], log)
        elif c.startswith('spacy-model:'):
            name, size, sha = SPACY_MODELS[c.split(':', 1)[1]]
            wheel = f'{name}-{SPACY_MODEL_VERSION}-py3-none-any.whl'
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / wheel
                _download(f'https://github.com/explosion/spacy-models/releases/download/{name}-{SPACY_MODEL_VERSION}/{wheel}',
                          path, size, sha)
                _pip(['--no-deps', str(path)], log)
        elif c == 'zero-shot-model':
            target = zero_shot_dir()
            for local, (remote, size, sha) in ZERO_SHOT['files'].items():
                if not (target / local).is_file():
                    _download(f'https://huggingface.co/{ZERO_SHOT["repo"]}/resolve/{ZERO_SHOT["revision"]}/{remote}',
                              target / local, size, sha)
        _marker(c).parent.mkdir(parents=True, exist_ok=True)
        _marker(c).write_text(json.dumps({'component': c, 'title': COMPONENTS[c]['title']}), encoding='utf-8')
        done.append(c)
    return done


def status() -> list[dict]:
    return [{'id': c, **meta, 'installed': installed(c)} for c, meta in COMPONENTS.items()]


def remove(component: str):
    if component in ('language-pack', 'neural-runtime'):
        shutil.rmtree(env_dir(ENV), ignore_errors=True)
        for c in COMPONENTS:
            if c in ('language-pack', 'neural-runtime') or c.startswith('spacy-model:'):
                _marker(c).unlink(missing_ok=True)
    elif component == 'zero-shot-model':
        shutil.rmtree(zero_shot_dir(), ignore_errors=True)
        _marker(component).unlink(missing_ok=True)
    else:
        _marker(component).unlink(missing_ok=True)


# =========================================================================== running workers

WORKERS = Path(__file__).with_name('addon_workers')


def run_worker(ctx, script: str, request: dict, timeout: int = 1800) -> dict:
    """Run an add-on worker script in the add-on environment with a JSON request; returns its JSON reply."""
    import time
    with tempfile.TemporaryDirectory(prefix='flowbench-addon-') as d:
        req, res = Path(d) / 'request.json', Path(d) / 'response.json'
        req.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        p = subprocess.Popen([str(env_python()), '-I', '-X', 'utf8', str(WORKERS / script), str(req), str(res)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
        deadline = time.monotonic() + timeout
        while p.poll() is None:
            if ctx.cancel_token.cancelled or time.monotonic() > deadline:
                p.kill()
                p.wait()
                ctx.check_cancelled()
                raise DataError(f'{script} exceeded {timeout} s and was stopped', code='timeout')
            time.sleep(0.05)
        _, err = p.communicate()
        if p.returncode != 0 or not res.is_file():
            raise DataError(f'The language worker failed: {err.strip()[-1500:]}')
        return json.loads(res.read_text(encoding='utf-8'))
