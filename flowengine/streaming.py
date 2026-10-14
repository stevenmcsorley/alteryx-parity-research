"""Batch streaming of linear pipeline segments in bounded memory.

A *segment* is a chain ``source -> t1 -> ... -> tk -> sink`` where the source
supports ``iter_batches``, every ``ti`` is streamable (``streaming`` is
'row-local' or 'stateful') with exactly one connected input and exactly one
consumed output edge, and the sink supports ``open_sink``. Row-local kernels
are applied per batch through their normal ``run`` method, so streaming and
batch execution share one implementation; stateful kernels implement
``open_stream``. Output digests are computed incrementally over the same
canonical row text as :meth:`Table.canonical_digest`, so a streamed run's
manifest is byte-identical to a batch run's.
"""
from __future__ import annotations

import hashlib
import json

from .errors import EngineError
from .table import Table
from .types import format_value


class DigestAccumulator:
    def __init__(self):
        self.h = None
        self.rows = 0
        self.schema = None

    def add(self, t: Table):
        if self.h is None:
            self.schema = t.schema
            self.h = hashlib.sha256(json.dumps(t.schema.to_list(), sort_keys=True).encode())
        elif tuple(t.schema) != tuple(self.schema):
            raise EngineError('Streamed batches changed schema mid-stream', code='internal-error')
        for row in t.rows():
            self.h.update(json.dumps([format_value(v) for v in row], ensure_ascii=False).encode())
            self.h.update(b'\n')
        self.rows += len(t)

    def finish(self, schema):
        if self.h is None:  # no batches at all: digest of an empty table
            return Table.empty(schema).canonical_digest(), 0, schema
        return self.h.hexdigest(), self.rows, self.schema


def find_segments(plan) -> list[list[str]]:
    consumers: dict[str, list] = {}
    for nid in plan.order:
        for anchor, ups in plan.nodes[nid].inputs.items():
            for u, ua in ups:
                consumers.setdefault(u, []).append((nid, anchor, ua))
    segments = []
    for nid in plan.order:
        pn = plan.nodes[nid]
        if nid in plan.skipped or pn.inputs or not hasattr(pn.tool, 'iter_batches') \
                or not pn.tool.can_stream(pn.config):
            continue
        chain = [nid]
        cur = nid
        while True:
            outs = consumers.get(cur, [])
            if len(outs) != 1:
                break
            nxt, _, _ = outs[0]
            npn = plan.nodes[nxt]
            if nxt in plan.skipped or len(npn.inputs) != 1 or len(next(iter(npn.inputs.values()))) != 1:
                break
            if hasattr(npn.tool, 'open_sink') and npn.tool.can_stream(npn.config):
                chain.append(nxt)
                segments.append(chain)
                break
            mode = getattr(npn.tool, 'streaming', None)
            if mode not in ('row-local', 'stateful') or not npn.tool.can_stream(npn.config):
                break
            chain.append(nxt)
            cur = nxt
    return segments


def run_segment(plan, chain, make_ctx, emit, batch_rows):
    """Execute one segment; returns {node_id: {anchor: (digest, rows, schema)}} for manifests."""
    src, *mids, sink = chain
    pnodes = [plan.nodes[n] for n in chain]
    ctxs = {n: make_ctx(n) for n in chain}
    # which output anchor of each node feeds the next node
    feed = {}
    for a, b in zip(chain, chain[1:]):
        (anchor_in, ups), = plan.nodes[b].inputs.items()
        feed[a] = (ups[0][1], anchor_in)
    acc = {n: {a.name: DigestAccumulator() for a in plan.nodes[n].tool.manifest.outputs} for n in chain[:-1]}
    stages = []
    for n in mids:
        tool = plan.nodes[n].tool
        stages.append((n, tool.open_stream(ctxs[n], plan.nodes[n].config) if tool.streaming == 'stateful' else None))
    sink_writer = None
    batches = 0
    for n in chain:
        emit('node-started', n, tool=plan.nodes[n].tool.manifest.tool_id,
             tool_version=plan.nodes[n].tool.manifest.version, streaming=True)
    try:
        for batch in pnodes[0].tool.iter_batches(ctxs[src], plan.nodes[src].config, batch_rows):
            batches += 1
            ctxs[src].check_cancelled()
            acc[src]['output'].add(batch)
            cur = batch
            prev = src
            for n, state in stages:
                out = state.process(cur) if state else plan.nodes[n].tool.run(ctxs[n], plan.nodes[n].config,
                                                                              {feed[prev][1]: cur})
                for a, t in out.items():
                    acc[n][a].add(t)
                cur = out[feed[n][0]]
                prev = n
            if sink_writer is None:
                sink_writer = plan.nodes[sink].tool.open_sink(ctxs[sink], plan.nodes[sink].config, cur.schema)
            sink_writer.write(cur)
        if sink_writer is None:  # empty source: still create the output (header only)
            last = chain[-2]
            last_schema = plan.schemas.get((last, feed[last][0]))
            if last_schema is None:
                raise EngineError('Cannot stream an empty input whose schema is unknown', code='data-error',
                                  node_id=src)
            sink_writer = plan.nodes[sink].tool.open_sink(ctxs[sink], plan.nodes[sink].config, last_schema)
        sink_writer.close()
    except BaseException:
        if sink_writer is not None:
            sink_writer.abort()
        raise
    stats = {}
    for n in chain[:-1]:
        stats[n] = {}
        for a, accu in acc[n].items():
            schema = plan.schemas.get((n, a))
            stats[n][a] = accu.finish(schema if schema is not None else accu.schema)
    stats[sink] = {}
    emit('info', src, message=f'streamed segment {" -> ".join(chain)} in {batches} batch(es) of <= {batch_rows} rows')
    return stats
