import { useMemo, useRef } from 'react';
import { useEditor } from '../state/store';
import type { FieldDef } from '../engine/types';
import type { CanvasNode, ToolNodeData } from '../model/workflow';
import { SchemaForm, type FieldContext } from './SchemaForm';
import { categoryColor, glyph } from '../ui/categories';

export function Inspector() {
  const selected = useEditor((s) => s.nodes.filter((n) => n.selected));
  if (selected.length > 1) return <MultiSelection count={selected.length} />;
  const node = selected[0];
  return (
    <aside className="app-right" aria-label="Configuration" data-testid="inspector">
      {!node ? <WorkflowProperties /> : node.data.kind === 'tool' ? <ToolInspector node={node} /> :
        node.data.kind === 'container' ? <ContainerInspector node={node} /> : <CommentInspector node={node} />}
    </aside>
  );
}

function MultiSelection({ count }: { count: number }) {
  const group = useEditor((s) => s.groupSelected);
  const del = useEditor((s) => s.deleteSelected);
  const dup = useEditor((s) => s.duplicateSelected);
  return (
    <aside className="app-right" aria-label="Configuration" data-testid="inspector">
      <div className="section"><h2>{count} items selected</h2></div>
      <div className="section" style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        <button onClick={() => group()}>Group in container (Ctrl+G)</button>
        <button onClick={() => dup()}>Duplicate (Ctrl+D)</button>
        <button className="danger" onClick={() => del()}>Delete</button>
      </div>
    </aside>
  );
}

function WorkflowProperties() {
  const name = useEditor((s) => s.name);
  const profile = useEditor((s) => s.profile);
  const profiles = useEditor((s) => s.profiles);
  const baseDir = useEditor((s) => s.baseDir);
  const filePath = useEditor((s) => s.filePath);
  const nodes = useEditor((s) => s.nodes);
  const problems = useEditor((s) => s.problems);
  const p = profiles.find((x) => x.name === profile);
  return (
    <>
      <div className="section"><h3>Workflow</h3><h2>{name}</h2></div>
      <div className="section kv">
        <label>File</label><span style={{ wordBreak: 'break-all' }}>{filePath ?? 'not saved'}</span>
        <label htmlFor="basedir">Base folder</label>
        <input id="basedir" type="text" value={baseDir} onChange={(e) => useEditor.getState().setFilePath(filePath, e.target.value)}
          title="Relative input/output paths resolve against this folder" />
        <label>Tools</label><span>{nodes.filter((n) => n.data.kind === 'tool').length}</span>
      </div>
      <div className="section">
        <h3>Compatibility profile</h3>
        <select value={profile} onChange={(e) => useEditor.getState().setProfile(e.target.value)} aria-label="Compatibility profile">
          {profiles.map((x) => <option key={x.name} value={x.name}>{x.name} ({x.status})</option>)}
        </select>
        {p && <p style={{ color: 'var(--text-2)', fontSize: 12 }}>{p.notes.join(' ')}</p>}
      </div>
      <div className="section">
        <h3>Validation</h3>
        {problems.length ? problems.map((pr, i) => <ProblemRow key={i} p={pr} />) : <div style={{ color: 'var(--ok)' }}>No problems found.</div>}
      </div>
    </>
  );
}

export function ProblemRow({ p }: { p: { code: string; message: string; node_id: string | null } }) {
  return (
    <div className="problem" role="alert">
      {p.node_id && <strong>{p.node_id}: </strong>}{p.message} <code>({p.code})</code>
    </div>
  );
}

function upstreamFields(node: CanvasNode): FieldContext {
  const s = useEditor.getState();
  const byAnchor: Record<string, FieldDef[]> = {};
  for (const e of s.edges.filter((x) => x.target === node.id)) {
    const fs = s.schemas[e.source]?.[e.sourceHandle ?? 'output'] ?? [];
    byAnchor[e.targetHandle ?? 'input'] = [...(byAnchor[e.targetHandle ?? 'input'] ?? []), ...fs];
  }
  const seen = new Set<string>();
  const all: FieldDef[] = [];
  for (const fs of Object.values(byAnchor)) for (const f of fs) if (!seen.has(f.name)) { seen.add(f.name); all.push(f); }
  return { all, byAnchor };
}

function ToolInspector({ node }: { node: CanvasNode }) {
  const d = node.data as ToolNodeData;
  const manifest = useEditor((s) => s.tools[d.tool]);
  const entry = useEditor((s) => s.catalog.find((c) => manifest?.catalog_ids.includes(c.id)));
  const problems = useEditor((s) => s.problems.filter((p) => p.node_id === node.id));
  const outSchemas = useEditor((s) => s.schemas[node.id]);
  const edges = useEditor((s) => s.edges);
  const schemasVersion = useEditor((s) => s.schemas);
  const update = useEditor((s) => s.updateNodeData);
  const checkpoint = useEditor((s) => s.checkpoint);
  const editing = useRef<string | null>(null);
  const fields = useMemo(() => upstreamFields(node), [node, edges, schemasVersion]);
  const beginEdit = () => {
    if (editing.current !== node.id) {
      checkpoint();
      editing.current = node.id;
    }
  };
  if (!manifest) {
    return <div className="section"><h2>{d.tool}</h2><div className="problem">No implementation is registered for this tool.</div></div>;
  }
  const color = categoryColor(entry?.category);
  return (
    <div onBlur={() => { editing.current = null; }}>
      <div className="section" style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
        <div className="glyph" style={{ background: color, color: '#fff', width: 30, height: 30, borderRadius: 7, display: 'grid', placeItems: 'center', fontWeight: 700 }}>{glyph(manifest.display_name)}</div>
        <div><h2>{manifest.display_name}</h2><div style={{ color: 'var(--text-3)', fontSize: 11 }}>{entry?.category} · {manifest.tool_id} v{manifest.version}</div></div>
      </div>
      <div className="section kv">
        <label>Node id</label><code>{node.id}</code>
        <label htmlFor="node-label">Label</label>
        <input id="node-label" type="text" value={d.label ?? ''} placeholder={manifest.display_name} onFocus={beginEdit}
          onChange={(e) => update(node.id, { label: e.target.value || undefined }, false)} />
        <label htmlFor="node-annot">Annotation</label>
        <input id="node-annot" type="text" value={d.annotation ?? ''} onFocus={beginEdit}
          onChange={(e) => update(node.id, { annotation: e.target.value || undefined }, false)} />
        <label htmlFor="node-disabled">Disabled</label>
        <input id="node-disabled" type="checkbox" checked={!!d.disabled} style={{ justifySelf: 'start' }}
          onChange={(e) => update(node.id, { disabled: e.target.checked || undefined })} />
      </div>
      {problems.length > 0 && <div className="section">{problems.map((p, i) => <ProblemRow key={i} p={p} />)}</div>}
      <div className="section" data-testid="config-form">
        <h3>Configuration</h3>
        {manifest.tool_id === 'core.text_input'
          ? <TextInputEditor value={d.config} onBeginEdit={beginEdit} onChange={(config) => update(node.id, { config }, false)} />
          : <SchemaForm schema={manifest.config_schema} value={d.config} fields={fields} idPrefix={`cfg-${node.id}`}
            onBeginEdit={beginEdit} onChange={(config) => update(node.id, { config }, false)} />}
        <p className="help" style={{ color: 'var(--text-3)', fontSize: 11 }}>{manifest.description}</p>
      </div>
      <div className="section">
        <h3>Anchors</h3>
        <table className="schema-table">
          <thead><tr><th>Anchor</th><th>Kind</th><th>Connections</th></tr></thead>
          <tbody>
            {manifest.inputs.map((a) => (
              <tr key={'i' + a.name}><td>{a.label} <code>{a.name}</code></td><td>input{a.multiple ? ' (many)' : ''}{a.required ? '' : ' (optional)'}</td>
                <td>{edges.filter((e) => e.target === node.id && e.targetHandle === a.name).length}</td></tr>
            ))}
            {manifest.outputs.map((a) => (
              <tr key={'o' + a.name}><td>{a.label} <code>{a.name}</code></td><td>output</td>
                <td>{edges.filter((e) => e.source === node.id && e.sourceHandle === a.name).length}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <SchemaPreview title="Incoming fields" groups={fields.byAnchor} />
      <SchemaPreview title="Outgoing fields" groups={outSchemas ?? {}} emptyText="Not known until upstream inputs are valid (or data-dependent)." />
    </div>
  );
}

function SchemaPreview({ title, groups, emptyText }: { title: string; groups: Record<string, FieldDef[]>; emptyText?: string }) {
  const entries = Object.entries(groups);
  return (
    <div className="section">
      <h3>{title}</h3>
      {!entries.length && <div style={{ color: 'var(--text-3)', fontSize: 12 }}>{emptyText ?? 'None connected.'}</div>}
      {entries.map(([anchor, fs]) => (
        <div key={anchor} style={{ marginBottom: 8 }}>
          {entries.length > 1 && <div style={{ fontWeight: 600, fontSize: 12 }}>{anchor}</div>}
          <table className="schema-table">
            <thead><tr><th>Field</th><th>Type</th><th>Size</th></tr></thead>
            <tbody>{fs.map((f) => <tr key={f.name}><td>{f.name}</td><td>{f.type}</td><td>{f.size ?? ''}{f.scale !== undefined ? `.${f.scale}` : ''}</td></tr>)}</tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

const TYPES = ['bool', 'byte', 'int16', 'int32', 'int64', 'fixed_decimal', 'float', 'double', 'string', 'wstring', 'v_string', 'v_wstring', 'date', 'time', 'datetime'];

function TextInputEditor({ value, onChange, onBeginEdit }: { value: Record<string, unknown>; onChange: (v: Record<string, unknown>) => void; onBeginEdit: () => void }) {
  const fields = (value.fields as { name: string; type: string }[]) ?? [];
  const rows = (value.rows as (string | number | boolean | null)[][]) ?? [];
  const set = (f: typeof fields, r: typeof rows) => onChange({ ...value, fields: f, rows: r });
  return (
    <div data-testid="text-input-editor" onFocus={onBeginEdit}>
      <div style={{ overflowX: 'auto' }}>
        <table className="schema-table">
          <thead>
            <tr>
              {fields.map((f, i) => (
                <th key={i} style={{ minWidth: 110 }}>
                  <input type="text" aria-label={`Field ${i + 1} name`} value={f.name} onChange={(e) => set(fields.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)), rows)} />
                  <select aria-label={`Field ${i + 1} type`} value={f.type} onChange={(e) => set(fields.map((x, j) => (j === i ? { ...x, type: e.target.value } : x)), rows)}>
                    {TYPES.map((t) => <option key={t}>{t}</option>)}
                  </select>
                  <button type="button" className="icon" aria-label={`Remove field ${i + 1}`} onClick={() => set(fields.filter((_, j) => j !== i), rows.map((r) => r.filter((_, j) => j !== i)))}>×</button>
                </th>
              ))}
              <th><button type="button" onClick={() => set([...fields, { name: `Field${fields.length + 1}`, type: 'v_wstring' }], rows.map((r) => [...r, null]))}>+ field</button></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                {fields.map((_, j) => (
                  <td key={j}>
                    <input type="text" aria-label={`Row ${i + 1} field ${j + 1}`} value={r[j] === null || r[j] === undefined ? '' : String(r[j])}
                      placeholder="null" onChange={(e) => set(fields, rows.map((rr, ii) => (ii === i ? rr.map((c, jj) => (jj === j ? (e.target.value === '' ? null : e.target.value) : c)) : rr)))} />
                  </td>
                ))}
                <td><button type="button" className="icon" aria-label={`Remove row ${i + 1}`} onClick={() => set(fields, rows.filter((_, ii) => ii !== i))}>×</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button type="button" style={{ marginTop: 6 }} onClick={() => set(fields, [...rows, fields.map(() => null)])} disabled={!fields.length}>+ row</button>
      <div className="help" style={{ color: 'var(--text-3)', fontSize: 11 }}>Empty cells are null. Values are converted to the field type when the workflow runs.</div>
    </div>
  );
}

function ContainerInspector({ node }: { node: CanvasNode }) {
  const d = node.data as { caption: string; disabled?: boolean };
  const update = useEditor((s) => s.updateNodeData);
  const ungroup = useEditor((s) => s.ungroup);
  const count = useEditor((s) => s.nodes.filter((n) => n.parentId === node.id).length);
  return (
    <>
      <div className="section"><h3>Container</h3><h2>{d.caption}</h2></div>
      <div className="section kv">
        <label htmlFor="cap">Caption</label>
        <input id="cap" type="text" value={d.caption} onChange={(e) => update(node.id, { caption: e.target.value }, false)} />
        <label htmlFor="cdis">Disabled</label>
        <input id="cdis" type="checkbox" style={{ justifySelf: 'start' }} checked={!!d.disabled} onChange={(e) => update(node.id, { disabled: e.target.checked })} />
        <label>Contents</label><span>{count} item(s)</span>
      </div>
      <div className="section"><p style={{ color: 'var(--text-2)', fontSize: 12 }}>Disabling a container disables every tool inside it and everything downstream of them.</p>
        <button onClick={() => ungroup(node.id)}>Ungroup</button></div>
    </>
  );
}

function CommentInspector({ node }: { node: CanvasNode }) {
  const d = node.data as { text: string };
  const update = useEditor((s) => s.updateNodeData);
  return (
    <>
      <div className="section"><h3>Comment</h3></div>
      <div className="section"><textarea rows={6} value={d.text} aria-label="Comment text" onChange={(e) => update(node.id, { text: e.target.value }, false)} /></div>
    </>
  );
}
