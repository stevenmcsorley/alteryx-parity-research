"""Shared by connectors that move files: read downloaded bytes as Input Data would, write records as Output Data would."""
from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path

from ..errors import ConfigError


def read_bytes(ctx, data: bytes, suffix: str, read_cfg: dict, label: str):
    """The table Input Data reads from these bytes, with the given Input Data settings."""
    from .inout import InputData
    if 'path' in read_cfg:
        raise ConfigError('read holds format settings; the file is named by the tool')
    reader = InputData()
    with tempfile.TemporaryDirectory(prefix='flowbench-remote-') as d:
        local = Path(d) / f'file{suffix}'
        local.write_bytes(data)
        ctx.info(f'Downloaded {label} ({len(data):,} bytes)')
        return reader.run(ctx, reader.normalize_config({**read_cfg, 'path': str(local)}), {})['output']


@contextlib.contextmanager
def written_file(ctx, write_cfg: dict, inputs: dict, suffix: str):
    """A temporary file holding the records exactly as Output Data writes them; yields its path."""
    from .inout import OutputData
    if 'path' in write_cfg:
        raise ConfigError('write holds format settings; the file is named by the tool')
    writer = OutputData()
    with tempfile.TemporaryDirectory(prefix='flowbench-remote-') as d:
        local = Path(d) / f'upload{suffix}'
        writer.run(ctx, writer.normalize_config({**write_cfg, 'path': str(local)}), inputs)
        yield local
