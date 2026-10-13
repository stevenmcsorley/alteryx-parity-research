import { useMemo, useState } from 'react';
import { useReactFlow } from '@xyflow/react';
import { useEditor } from '../state/store';
import { exportText, previewToCsv } from '../state/engineActions';
import { ProblemRow } from './Inspector';
import { notify } from '../ui/toast';
import { promptPath } from '../ui/dialogs';

export function ResultsPanel() {
  const tab = useEditor((s) => s.bottomTab);
  const setTab = useEditor((s) => s.setBottomTab);
  const run = useEditor((s) => s.run);
  const problems = useEditor((s) => s.problems);
  const warnings = run.events.filter((e) => e.type === 'warning').length;
  const onResize = (e: React.PointerEvent) => {
    const startY = e.clientY;
    const app = document.querySelector('.app') as HTMLElement;
    const start = parseInt(getComputedStyle(app).getPropertyValue('--bottom-h') || '260', 10);
    const move = (ev: PointerEvent) => app.style.setProperty('--bottom-h', `${Math.max(120, Math.min(window.innerHeight - 200, start + startY - ev.clientY))}px`);
    const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };
  return (
    <section className="app-bottom" aria-label="Results and messages">
      <div className="resizer" onPointerDown={onResize} role="separator" aria-orientation="horizontal" aria-label="Resize results panel" />
      <div className="tabs" role="tablist">
        {(['results', 'fields', 'messages', 'performance', 'history'] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)} data-testid={`tab-${t}`}>
            {t === 'messages' ? `Messages${problems.length || warnings ? ` (${problems.length + warnings})` : ''}` : t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
        <span className="spacer" />
        <RunSummary />
      </div>
      <div className="pane">
        {tab === 'results' && <ResultsGrid />}
        {tab === 'fields' && <FieldsTab />}
        {tab === 'messages' && <Messages />}
        {tab === 'performance' && <Performance />}
        {tab === 'history' && <HistoryTab />}
      </div>
    </section>
  );
}

function RunSummary() {
  const run = useEditor((s) => s.run);
  if (run.status === 'idle') return null;
  const wall = run.result?.manifest?.stats?.wall_ms;
  const cls = run.status === 'succeeded' ? 'ok' : run.status === 'running' ? 'run' : run.status === 'cancelled' ? 'muted' : 'err';
  return <span className={`status-pill ${cls}`} style={{ alignSelf: 'center' }} data-testid="run-status">
    {run.status}{wall !== undefined ? ` · ${(wall / 1000).toFixed(2)} s` : ''}</span>;
}

function useOutputs() {
  const result = useEditor((s) => s.run.result);
  return useMemo(() => {
    const list: { node: string; anchor: string; rows: number }[] = [];
    for (const [node, anchors] of Object.entries(result?.previews ?? {}))
      for (const [anchor, p] of Object.entries(anchors)) list.push({ node, anchor, rows: p.total_rows });
    for (const [node, p] of Object.entries(result?.browse ?? {})) list.push({ node, anchor: '(browse)', rows: p.total_rows });
    return list;
  }, [result]);
}

function selectedPreview() {
  const s = useEditor.getState();
  const sel = s.selectedResult;
  if (!sel || !s.run.result) return null;
  return sel.anchor === '(browse)' ? s.run.result.browse[sel.node] : s.run.result.previews[sel.node]?.[sel.anchor];
}

function OutputPicker() {
  const outputs = useOutputs();
  const sel = useEditor((s) => s.selectedResult);
  const selectResult = useEditor((s) => s.selectResult);
  return (
    <select aria-label="Output to display" value={sel ? `${sel.node}::${sel.anchor}` : ''}
      onChange={(e) => { const [n, a] = e.target.value.split('::'); selectResult(n, a); }}>
      {!sel && <option value="">Choose an output…</option>}
      {outputs.map((o) => <option key={`${o.node}::${o.anchor}`} value={`${o.node}::${o.anchor}`}>{o.node}.{o.anchor} — {o.rows.toLocaleString()} rows</option>)}
    </select>
  );
}

function ResultsGrid() {
  const run = useEditor((s) => s.run);
  useEditor((s) => s.selectedResult);
  const [filter, setFilter] = useState('');
  const p = selectedPreview();
  if (run.status === 'idle') return <div className="empty">Run the workflow (F5) to see results for every output anchor.</div>;
  if (run.status === 'running' && !run.result) return <div className="empty">Running…</div>;
  if (!p) return <div className="results-bar"><OutputPicker /><span style={{ color: 'var(--text-3)' }}>No retained output (streamed or failed before producing data).</span></div>;
  const needle = filter.toLowerCase();
  const rows = needle ? p.rows.filter((r) => r.some((v) => v !== null && v.toLowerCase().includes(needle))) : p.rows;
  const doExport = async () => {
    const sel = useEditor.getState().selectedResult!;
    const path = await promptPath('Export preview rows as CSV', `${useEditor.getState().baseDir}${sep()}${sel.node}_${sel.anchor}.csv`, 'csv');
    if (!path) return;
    try {
      notify(`Exported ${p.rows.length} rows to ${await exportText(path, previewToCsv(p.fields, p.rows))}`);
    } catch (e: any) {
      notify(e.message);
    }
  };
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="results-bar">
        <OutputPicker />
        <span data-testid="result-count">{p.total_rows.toLocaleString()} rows{p.rows.length < p.total_rows ? ` (showing first ${p.rows.length.toLocaleString()})` : ''} · {p.fields.length} fields</span>
        <input type="search" placeholder="Filter rows…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 180 }} aria-label="Filter rows" />
        <span style={{ flex: 1 }} />
        <button onClick={doExport} title="Writes the rows shown here; use an Output Data tool to export complete results">Export preview…</button>
      </div>
      <div className="grid-wrap">
        <table className="grid" data-testid="results-grid">
          <thead><tr><th>#</th>{p.fields.map((f) => <th key={f.name}>{f.name}<span className="ftype">{f.type}</span></th>)}</tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}><td className="rownum">{i + 1}</td>{r.map((v, j) => v === null ? <td key={j} className="null">[Null]</td> : <td key={j} title={v}>{v}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function sep() { return navigator.userAgent.includes('Windows') ? '\\' : '/'; }

function FieldsTab() {
  useEditor((s) => s.selectedResult);
  useEditor((s) => s.run.result);
  const p = selectedPreview();
  if (!p) return <div className="empty">Select an output in the Results tab to inspect its field metadata.</div>;
  return (
    <div style={{ padding: 8 }}>
      <div className="results-bar" style={{ border: 0, padding: '0 0 6px' }}><OutputPicker /></div>
      <table className="schema-table" style={{ maxWidth: 640 }} data-testid="fields-table">
        <thead><tr><th>#</th><th>Field</th><th>Type</th><th>Size</th><th>Scale</th><th>Nulls in preview</th></tr></thead>
        <tbody>{p.fields.map((f, i) => (
          <tr key={f.name}><td>{i + 1}</td><td>{f.name}</td><td>{f.type}</td><td>{f.size ?? ''}</td><td>{f.scale ?? ''}</td>
            <td>{p.rows.filter((r) => r[i] === null).length}</td></tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Messages() {
  const problems = useEditor((s) => s.problems);
  const events = useEditor((s) => s.run.events);
  const error = useEditor((s) => s.run.result?.error);
  const flow = useReactFlow();
  const [level, setLevel] = useState<'all' | 'warnings'>('all');
  const focus = (id: string | null) => {
    if (!id) return;
    useEditor.setState({ nodes: useEditor.getState().nodes.map((n) => ({ ...n, selected: n.id === id })) });
    flow.fitView({ nodes: [{ id }], duration: 300, maxZoom: 1.3 });
  };
  const report = useEditor((s) => s.importReport);
  const shown = events.filter((e) => level === 'all' || ['warning', 'node-failed', 'run-cancelled'].includes(e.type));
  return (
    <div data-testid="messages">
      <div style={{ padding: 8 }}>
        {error && <div className="problem" role="alert" data-testid="run-error"><strong>Run failed at {error.node_id}</strong>: {error.message} <code>({error.code})</code> <button onClick={() => focus(error.node_id)}>Show node</button></div>}
        {report && (
          <div className="section" data-testid="import-report" style={{ padding: '6px 0' }}>
            <strong>Imported {report.source}</strong> (format {report.source_format_version}):{' '}
            {Object.entries(report.counts).map(([k, v]) => `${v} ${k}`).join(', ')}
            {report.nodes.filter((n: any) => n.status !== 'mapped').map((n: any) => (
              <div key={n.node} className="log-row warning" onClick={() => focus(n.node)}>
                <span className="t">{n.status}</span><span>{n.node}</span><span>{String(n.source).split('.').pop()}</span>
                <span>{n.reason ?? (n.warnings ?? []).join('; ')}</span>
              </div>
            ))}
          </div>
        )}
        {problems.map((p, i) => <div key={i} onClick={() => focus(p.node_id)} style={{ cursor: p.node_id ? 'pointer' : undefined }}><ProblemRow p={p} /></div>)}
      </div>
      <div className="results-bar">
        <strong style={{ fontSize: 12 }}>Execution log</strong>
        <select value={level} onChange={(e) => setLevel(e.target.value as any)} aria-label="Log level"><option value="all">All events</option><option value="warnings">Warnings and errors</option></select>
      </div>
      {shown.map((e) => (
        <div key={e.seq} className={`log-row ${e.type === 'warning' ? 'warning' : e.type.includes('failed') ? 'error' : ''}`}>
          <span className="t">{e.ts.slice(11, 23)}</span>
          <span>{e.type}</span>
          <span>{e.node_id ? <a onClick={() => focus(e.node_id)}>{e.node_id}</a> : ''}</span>
          <span>{e.message ?? (e.output_rows ? `rows ${JSON.stringify(e.output_rows)} in ${e.duration_ms} ms` : e.tool ? String(e.tool) : '')}</span>
        </div>
      ))}
      {!shown.length && !problems.length && <div className="empty">No messages.</div>}
    </div>
  );
}

function Performance() {
  const result = useEditor((s) => s.run.result);
  const nodeMs = useEditor((s) => s.run.nodeMs);
  if (!result?.manifest) return <div className="empty">Run statistics appear after a run.</div>;
  const st = result.manifest.stats ?? {};
  const nodes = Object.entries(result.manifest.nodes as Record<string, any>);
  const total = nodes.reduce((a, [, n]) => a + (n.duration_ms ?? 0), 0) || 1;
  return (
    <div style={{ padding: 8 }} data-testid="performance">
      <div className="kv" style={{ maxWidth: 560, marginBottom: 10 }}>
        <label>Wall time</label><span>{st.wall_ms} ms</span>
        <label>Workers</label><span>{st.workers}</span>
        <label>Profile</label><span>{result.manifest.profile?.name}</span>
        <label>Spills</label><span>{st.spills ?? 0} (peak resident ≈ {((st.peak_resident_bytes_estimate ?? 0) / 1024).toFixed(0)} KiB)</span>
      </div>
      <table className="schema-table" style={{ maxWidth: 760 }}>
        <thead><tr><th>Node</th><th>Tool</th><th>Duration (ms)</th><th>Share</th><th>Rows out</th></tr></thead>
        <tbody>{nodes.sort((a, b) => (b[1].duration_ms ?? 0) - (a[1].duration_ms ?? 0)).map(([id, n]) => (
          <tr key={id}><td>{id}</td><td>{n.tool}{n.streamed ? ' (streamed)' : ''}</td><td>{(nodeMs[id] ?? n.duration_ms ?? 0).toFixed(2)}</td>
            <td><div style={{ background: 'var(--accent-soft)', width: 120 }}><div style={{ background: 'var(--accent)', height: 8, width: `${(100 * (n.duration_ms ?? 0)) / total}%` }} /></div></td>
            <td>{Object.entries(n.outputs ?? {}).map(([a, o]: any) => `${a}: ${o.rows}`).join(', ')}</td></tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function HistoryTab() {
  const history = useEditor((s) => s.history);
  if (!history.length) return <div className="empty">No recorded runs yet. The desktop app records runs in a local history database.</div>;
  return (
    <table className="schema-table" style={{ margin: 8, maxWidth: 900 }} data-testid="history">
      <thead><tr><th>Started (UTC)</th><th>Status</th><th>Workflow</th><th>Profile</th><th>Duration</th><th>Run id</th></tr></thead>
      <tbody>{history.map((h) => (
        <tr key={h.run_id}><td>{h.started_utc.replace('T', ' ').slice(0, 19)}</td><td>{h.status}</td><td>{h.workflow_name}</td><td>{h.profile}</td>
          <td>{h.duration_ms ? `${h.duration_ms.toFixed(0)} ms` : ''}</td><td><code>{h.run_id.slice(0, 12)}</code></td></tr>
      ))}</tbody>
    </table>
  );
}
