import { useMemo, useState } from 'react';
import { useReactFlow } from '@xyflow/react';
import { useEditor } from '../state/store';
import type { CatalogEntry } from '../engine/types';
import { categoryColor } from '../ui/categories';
import { TOOL_MIME } from '../canvas/Canvas';

export function statusOf(c: CatalogEntry): { cls: string; label: string; text: string } {
  // Labels follow the evidence ledger (implementation_status), never mere kernel presence.
  if (c.id === 'interface:condition')
    return { cls: 'tested', label: 'Flowbench native', text: 'Native analytic-app Condition routing; unsupported imported Alteryx Condition XML remains preserved with diagnostics.' };
  if (c.kernel && c.implementation_status !== 'implemented')
    return { cls: 'blocked', label: 'In progress', text: 'A kernel exists but has not yet passed the evidence rules (tests); results may change.' };
  if (c.kernel && c.verification_status === 'partial')
    return { cls: 'tested', label: 'Ref-tested', text: 'Implemented; every recorded reference comparison passes (partial coverage of options).' };
  if (c.kernel && c.verification_status === 'failed')
    return { cls: 'refdiff', label: 'Ref-diff', text: 'Implemented, but a reference comparison currently differs (see the contract).' };
  if (c.kernel) return { cls: 'ready', label: 'Ready', text: 'Implemented and tested in this project; not yet compared with the reference.' };
  if (c.external_prerequisites_blocking) return { cls: 'blocked', label: 'Blocked', text: c.unavailable_reason ?? '' };
  return { cls: 'unavailable', label: 'Unavailable', text: c.unavailable_reason ?? '' };
}

export function Palette() {
  const catalog = useEditor((s) => s.catalog);
  const tools = useEditor((s) => s.tools);
  const [tab, setTab] = useState<'tools' | 'navigator'>('tools');
  const [q, setQ] = useState('');
  const [cat, setCat] = useState('');
  const [onlyReady, setOnlyReady] = useState(false);
  const [active, setActive] = useState<string | null>(null);
  const addTool = useEditor((s) => s.addTool);
  const addApplicationAction = useEditor((s) => s.addApplicationAction);
  const addApplicationCondition = useEditor((s) => s.addApplicationCondition);
  const flow = useReactFlow();

  const categories = useMemo(() => [...new Set(catalog.map((c) => c.category))].sort(), [catalog]);
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return catalog.filter((c) => (!cat || c.category === cat) && (!onlyReady || (c.kernel && c.implementation_status === 'implemented')) &&
      (!needle || c.name.toLowerCase().includes(needle) || c.category.toLowerCase().includes(needle) ||
        (c.kernel && tools[c.kernel]?.description.toLowerCase().includes(needle))));
  }, [catalog, q, cat, onlyReady, tools]);
  const grouped = useMemo(() => {
    const m = new Map<string, CatalogEntry[]>();
    for (const c of filtered) m.set(c.category, [...(m.get(c.category) ?? []), c]);
    return [...m.entries()];
  }, [filtered]);
  const implemented = catalog.filter((c) => c.kernel && c.implementation_status === 'implemented').length;
  const activeEntry = catalog.find((c) => c.id === active);
  const interfaceQuestions: Record<string, string> = { 'interface:action': 'text', 'interface:control-parameter': 'control-parameter', 'interface:text-box': 'text-box', 'interface:check-box': 'check-box', 'interface:drop-down': 'drop-down', 'interface:numeric-up-down': 'numeric-up-down', 'interface:radio-button': 'radio-button', 'interface:file-browse': 'file-browse', 'interface:folder-browse': 'folder-browse' };
  const addInterfaceQuestion = (catalogId: string, position?: { x: number; y: number }) => {
    const selected = useEditor.getState().nodes.find((n) => n.selected);
    const p = position ?? (selected ? { x: selected.position.x, y: selected.position.y + 190 } : { x: 120, y: 120 });
    if (catalogId === 'interface:condition') { addApplicationCondition(p); return; }
    addApplicationAction(p, interfaceQuestions[catalogId] ?? 'text', catalog.find((c) => c.id === catalogId)?.name ?? 'Enter a value');
  };
  const canAddInterface = (id: string) => id in interfaceQuestions || id === 'interface:condition';
  const extensions = ['core.db_input', 'core.db_output'].filter((id) => tools[id] &&
    (!q.trim() || tools[id].display_name.toLowerCase().includes(q.trim().toLowerCase()) ||
      tools[id].description.toLowerCase().includes(q.trim().toLowerCase())));
  const macroExtensions = ['flowengine.macro', 'flowengine.batch_macro', 'flowengine.iterative_macro'].filter((id) => tools[id] &&
    (!q.trim() || tools[id].display_name.toLowerCase().includes(q.trim().toLowerCase()) ||
      tools[id].description.toLowerCase().includes(q.trim().toLowerCase())));

  const addAtCentre = (kernel: string) => {
    // place to the right of the selected (or most recently added) top-level tool, else at the view centre
    const nodes = useEditor.getState().nodes.filter((n) => n.type === 'tool' && !n.parentId);
    const anchor = nodes.find((n) => n.selected) ?? nodes[nodes.length - 1];
    if (anchor) {
      const w = Number(anchor.measured?.width ?? 180);
      if (kernel === 'flowengine.application_action') addApplicationAction({ x: Math.round(anchor.position.x), y: Math.round(anchor.position.y + 190) });
      else addTool(kernel, { x: Math.round(anchor.position.x + w + 70), y: Math.round(anchor.position.y) });
      return;
    }
    const r = document.querySelector('[data-testid="canvas"]')?.getBoundingClientRect();
    const p = flow.screenToFlowPosition({ x: (r?.left ?? 0) + (r?.width ?? 600) / 3, y: (r?.top ?? 0) + (r?.height ?? 400) / 3 });
    if (kernel === 'flowengine.application_action') addApplicationAction({ x: Math.round(p.x), y: Math.round(p.y) });
    else addTool(kernel, { x: Math.round(p.x), y: Math.round(p.y) });
  };

  return (
    <aside className="app-left" aria-label="Tool palette">
      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === 'tools'} onClick={() => setTab('tools')}>Tools</button>
        <button role="tab" aria-selected={tab === 'navigator'} onClick={() => setTab('navigator')}>Navigator</button>
      </div>
      {tab === 'navigator' ? <Navigator /> : (
        <>
          <div className="palette-head">
            <input type="search" placeholder={`Search ${catalog.length} catalogue tools…`} value={q}
              onChange={(e) => setQ(e.target.value)} aria-label="Search tools" />
            <div style={{ display: 'flex', gap: 6 }}>
              <select value={cat} onChange={(e) => setCat(e.target.value)} aria-label="Category filter">
                <option value="">All categories ({categories.length})</option>
                {categories.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <input type="checkbox" checked={onlyReady} onChange={(e) => setOnlyReady(e.target.checked)} />
              Implemented only ({implemented} of {catalog.length})
            </label>
          </div>
          <div className="palette-list" data-testid="palette-list">
            {grouped.map(([category, items]) => (
              <div key={category}>
                <div className="cat-head"><span>{category}</span><span>{items.filter((i) => i.kernel && i.implementation_status === 'implemented').length}/{items.length}</span></div>
                {items.map((c) => {
                  const st = statusOf(c);
                  return (
                    <div key={c.id} className={`tool-item ${c.kernel ? '' : 'unavailable'} ${active === c.id ? 'active' : ''}`}
                      data-testid={`palette-${c.id}`} role="button" tabIndex={0}
                      draggable={!!c.kernel || canAddInterface(c.id)}
                      onDragStart={(e) => {
                        if (!c.kernel && !canAddInterface(c.id)) return;
                        const special = c.id === 'interface:condition' ? 'flowengine.application_condition' : `flowengine.application_action:${c.id}`;
                        e.dataTransfer.setData(TOOL_MIME, c.kernel ?? special);
                        e.dataTransfer.effectAllowed = 'copy';
                      }}
                      onClick={() => setActive(c.id)}
                      onDoubleClick={() => canAddInterface(c.id) ? addInterfaceQuestion(c.id) : c.kernel && addAtCentre(c.kernel)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && (c.kernel || canAddInterface(c.id))) canAddInterface(c.id) ? addInterfaceQuestion(c.id) : addAtCentre(c.kernel!);
                        if (e.key === ' ') setActive(c.id);
                      }}
                      title={st.text}>
                      <span className="cat-dot" style={{ background: categoryColor(c.category) }} aria-hidden />
                      <span className="tool-name">{c.name}</span>
                      <span className={`badge ${st.cls}`}>{st.label}</span>
                    </div>
                  );
                })}
              </div>
            ))}
            {extensions.length > 0 && <div>
              <div className="cat-head"><span>Database extensions</span><span>{extensions.length}</span></div>
              {extensions.map((id) => <div key={id} data-testid={`palette-extension-${id}`} className={`db-tool-item ${active === id ? 'active' : ''}`} role="button" tabIndex={0}
                draggable onDragStart={(e) => { e.dataTransfer.setData(TOOL_MIME, id); e.dataTransfer.effectAllowed = 'copy'; }}
                onClick={() => setActive(id)} onDoubleClick={() => addAtCentre(id)}
                onKeyDown={(e) => e.key === 'Enter' && addAtCentre(id)}>
                <span className="cat-dot" style={{ background: '#277da1' }} aria-hidden />
                <span className="tool-name">{tools[id].display_name}</span><span className="badge ready">Ready</span>
              </div>)}
            </div>}
            {macroExtensions.length > 0 && <div>
              <div className="cat-head"><span>Macro runtimes</span><span>{macroExtensions.length}</span></div>
              {macroExtensions.map((id) => <div key={id} data-testid={`palette-extension-${id}`} className={`db-tool-item ${active === id ? 'active' : ''}`} role="button" tabIndex={0}
                draggable onDragStart={(e) => { e.dataTransfer.setData(TOOL_MIME, id); e.dataTransfer.effectAllowed = 'copy'; }}
                onClick={() => setActive(id)} onDoubleClick={() => addAtCentre(id)}
                onKeyDown={(e) => e.key === 'Enter' && addAtCentre(id)}>
                <span className="cat-dot" style={{ background: '#6a4c93' }} aria-hidden />
                <span className="tool-name">{tools[id].display_name}</span><span className="badge ready">Ready</span>
              </div>)}
            </div>}
            {!grouped.length && !extensions.length && !macroExtensions.length && <div className="empty">No tools match.</div>}
          </div>
          {activeEntry && <ToolDetail entry={activeEntry} onAdd={addAtCentre} onAddQuestion={addInterfaceQuestion} supportsQuestion={activeEntry.id in interfaceQuestions} />}
        </>
      )}
    </aside>
  );
}

function ToolDetail({ entry, onAdd, onAddQuestion, supportsQuestion }: { entry: CatalogEntry; onAdd: (k: string) => void; onAddQuestion: (catalogId: string) => void; supportsQuestion: boolean }) {
  const m = useEditor((s) => (entry.kernel ? s.tools[entry.kernel] : undefined));
  const st = statusOf(entry);
  return (
    <div className="tool-detail" data-testid="tool-detail">
      <h4>{entry.name} <span className={`badge ${st.cls}`}>{st.label}</span></h4>
      <div style={{ color: 'var(--text-3)', fontSize: 11 }}>{entry.category} · <code>{entry.id}</code></div>
      <p>{m?.description ?? st.text}</p>
      {m && (
        <p>
          Inputs: {m.inputs.map((a) => a.label + (a.multiple ? '…' : '')).join(', ') || 'none'} · Outputs: {m.outputs.map((a) => a.label).join(', ') || 'none'}
          <br />Kernel <code>{m.tool_id}</code> v{m.version}{m.side_effects !== 'none' ? ` · ${m.side_effects}` : ''}
        </p>
      )}
      {m && <p>{st.text}</p>}
      {entry.documentation_url && <p style={{ wordBreak: 'break-all' }}>Public reference: {entry.documentation_url}</p>}
      {m && <button className="primary" onClick={() => onAdd(m.tool_id)}>Add to canvas</button>}
      {!m && supportsQuestion && <button className="primary" onClick={() => onAddQuestion(entry.id)}>Add application question</button>}
    </div>
  );
}

function Navigator() {
  const nodes = useEditor((s) => s.nodes);
  const tools = useEditor((s) => s.tools);
  const status = useEditor((s) => s.run.nodeStatus);
  const problems = useEditor((s) => s.problems);
  const flow = useReactFlow();
  const [q, setQ] = useState('');
  const list = nodes.filter((n) => !q || n.id.toLowerCase().includes(q.toLowerCase()) ||
    (n.data.kind === 'tool' && (tools[n.data.tool]?.display_name ?? '').toLowerCase().includes(q.toLowerCase())));
  const focus = (id: string) => {
    useEditor.setState({ nodes: useEditor.getState().nodes.map((n) => ({ ...n, selected: n.id === id })) });
    flow.fitView({ nodes: [{ id }], duration: 300, maxZoom: 1.3 });
  };
  return (
    <div className="pane" data-testid="navigator">
      <div className="palette-head"><input type="search" placeholder="Find node…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Find node" /></div>
      {list.map((n) => {
        const label = n.data.kind === 'tool' ? (n.data.label || tools[n.data.tool]?.display_name || n.data.tool)
          : n.data.kind === 'container' ? `▢ ${n.data.caption}` : '✎ Comment';
        const bad = problems.some((p) => p.node_id === n.id);
        return (
          <div key={n.id} className="tool-item" role="button" tabIndex={0} onClick={() => focus(n.id)}
            onKeyDown={(e) => e.key === 'Enter' && focus(n.id)}>
            {status[n.id] && <span className={`run-dot ${status[n.id]}`} aria-hidden />}
            <span className="tool-name">{label} <span style={{ color: 'var(--text-3)', fontFamily: 'var(--mono)', fontSize: 11 }}>{n.id}</span></span>
            {bad && <span className="badge blocked">problem</span>}
          </div>
        );
      })}
      {!list.length && <div className="empty">No nodes on the canvas yet.</div>}
    </div>
  );
}
