import { useEffect, useRef, useState } from 'react';
import { isTauri } from '../engine/transport';
import { engine } from '../state/engineActions';

type Request = { title: string; initial: string; ext: string; mode: 'save' | 'open'; resolve: (v: string | null) => void };
export type AppQuestion = { id: string; label?: string; type: 'text' | 'text-area' | 'boolean' | 'number' | 'choice' | 'multi-choice' | 'date' | 'file' | 'folder' | 'tree' | 'map' | 'connection'; kinds?: string[]; default?: unknown; required?: boolean; options?: unknown[]; description?: string; secret?: boolean; minimum?: number; maximum?: number; mode?: 'open' | 'save'; extensions?: string[] };
type AppRequest = { title: string; questions: AppQuestion[]; resolve: (v: Record<string, unknown> | null) => void };
let host: ((r: Request) => void) | null = null;
let appHost: ((r: AppRequest) => void) | null = null;

/** Ask for a file path. Uses native dialogs in the desktop shell, an in-app dialog in the browser build. */
export async function promptPath(title: string, initial: string, ext: string, mode: 'save' | 'open' = 'save'): Promise<string | null> {
  if (isTauri()) {
    const dlg = await import('@tauri-apps/plugin-dialog');
    const filters = [{ name: ext === 'flow.json' ? 'Workflow' : ext.toUpperCase(), extensions: [ext === 'flow.json' ? 'json' : ext] }];
    const res = mode === 'save' ? await dlg.save({ title, defaultPath: initial, filters }) : await dlg.open({ title, defaultPath: initial, filters, multiple: false });
    return typeof res === 'string' ? res : null;
  }
  return new Promise((resolve) => {
    if (host) host({ title, initial, ext, mode, resolve });
    else resolve(window.prompt(title, initial));
  });
}

export async function promptApplication(questions: AppQuestion[]): Promise<Record<string, unknown> | null> {
  if (!questions.length) return {};
  return new Promise((resolve) => appHost ? appHost({ title: 'Run analytic application', questions, resolve }) : resolve(null));
}

export function DialogHost() {
  const [req, setReq] = useState<Request | null>(null);
  const [appReq, setAppReq] = useState<AppRequest | null>(null);
  const [value, setValue] = useState('');
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    host = (r) => { setReq(r); setValue(r.initial); };
    appHost = (r) => setAppReq(r);
    return () => { host = null; appHost = null; };
  }, []);
  useEffect(() => { if (req) input.current?.select(); }, [req]);
  if (appReq) return <ApplicationDialog request={appReq} close={(v) => { appReq.resolve(v); setAppReq(null); }} />;
  if (!req) return null;
  const close = (v: string | null) => { req.resolve(v); setReq(null); };
  return (
    <div className="modal-backdrop" onMouseDown={() => close(null)}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={req.title} onMouseDown={(e) => e.stopPropagation()}>
        <h3>{req.title}</h3>
        <label htmlFor="path-input">Path ({req.mode === 'save' ? 'will be created or replaced' : 'existing file'})</label>
        <input id="path-input" ref={input} type="text" value={value} onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') close(value.trim() || null); if (e.key === 'Escape') close(null); }} />
        <div className="actions">
          <button onClick={() => close(null)}>Cancel</button>
          <button className="primary" onClick={() => close(value.trim() || null)} data-testid="dialog-ok">{req.mode === 'save' ? 'Save' : 'Open'}</button>
        </div>
      </div>
    </div>
  );
}

function ApplicationDialog({ request, close }: { request: AppRequest; close: (v: Record<string, unknown> | null) => void }) {
  const [values, setValues] = useState<Record<string, unknown>>(() => Object.fromEntries(request.questions.map((q) => [q.id, q.default ?? (q.type === 'boolean' ? false : q.type === 'number' ? q.minimum ?? 0 : q.type === 'choice' ? q.options?.[0] ?? '' : q.type === 'multi-choice' ? [] : '')])));
  const [error, setError] = useState('');
  const choosePath = async (q: AppQuestion) => {
    try {
      let selected: string | null = null;
      if (isTauri()) {
        const dlg = await import('@tauri-apps/plugin-dialog');
        const filters = q.type === 'file' && q.extensions?.length ? [{ name: q.extensions.map((x) => `.${x}`).join(', '), extensions: q.extensions }] : undefined;
        const result = q.type === 'file' && q.mode === 'save'
          ? await dlg.save({ title: q.label || q.id, filters })
          : await dlg.open({ title: q.label || q.id, directory: q.type === 'folder', multiple: false, filters });
        selected = typeof result === 'string' ? result : null;
      } else selected = window.prompt(q.type === 'folder' ? 'Choose a folder path' : 'Choose a file path', String(values[q.id] ?? ''));
      if (selected !== null) setValues((old) => ({ ...old, [q.id]: selected }));
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };
  const submit = () => {
    const missing = request.questions.find((q) => q.required && (values[q.id] === '' || values[q.id] == null || (q.type === 'multi-choice' && Array.isArray(values[q.id]) && (values[q.id] as unknown[]).length === 0)));
    if (missing) { setError(`${missing.label || missing.id} is required.`); return; }
    const outOfRange = request.questions.find((q) => q.type === 'number' && values[q.id] != null && ((q.minimum != null && Number(values[q.id]) < q.minimum) || (q.maximum != null && Number(values[q.id]) > q.maximum)));
    if (outOfRange) { setError(`${outOfRange.label || outOfRange.id} is outside the allowed range.`); return; }
    close(Object.fromEntries(request.questions.map((q) => [q.id, q.type === 'map' ? mapAnswer(values[q.id]) : values[q.id]])));
  };
  return <div className="modal-backdrop" onMouseDown={() => close(null)}>
    <div className="modal" role="dialog" aria-modal="true" aria-label={request.title} onMouseDown={(e) => e.stopPropagation()}>
      <h3>{request.title}</h3>
      {request.questions.map((q) => q.type === 'tree' ? <div key={q.id} style={{ display: 'block', margin: '12px 0' }}>
        <span id={`tree-label-${q.id}`}>{q.label || q.id}{q.required ? ' *' : ''}</span>
        <div role="tree" aria-labelledby={`tree-label-${q.id}`} className="application-tree">{buildTree(q.options ?? []).map((entry) => <TreeChoice key={entry.label} item={entry} selected={String(values[q.id] ?? '')}
          onSelect={(value) => setValues((old) => ({ ...old, [q.id]: value }))} />)}</div>
        <small aria-live="polite">{String(values[q.id] ?? '')}</small>
        {q.description && <small style={{ display: 'block' }}>{q.description}</small>}
      </div> : <label key={q.id} style={{ display: 'block', margin: '12px 0' }}>
        <span>{q.label || q.id}{q.required ? ' *' : ''}</span>
        {q.type === 'boolean' ? <input aria-label={q.label || q.id} type="checkbox" checked={Boolean(values[q.id])}
          onChange={(e) => setValues({ ...values, [q.id]: e.target.checked })} />
          : q.type === 'choice' || q.type === 'multi-choice' ? <select aria-label={q.label || q.id} multiple={q.type === 'multi-choice'} value={q.type === 'multi-choice' ? (values[q.id] as string[] ?? []) : String(values[q.id] ?? '')}
            onChange={(e) => setValues({ ...values, [q.id]: q.type === 'multi-choice' ? [...e.target.selectedOptions].map((o) => o.value) : e.target.value })}>{(q.options ?? []).map((option) => <option key={String(option)} value={String(option)}>{String(option)}</option>)}</select>
            : q.type === 'connection' ? <ConnectionChoice question={q} value={String(values[q.id] ?? '')} onChange={(v) => setValues({ ...values, [q.id]: v })} />
            : q.type === 'map' ? <textarea aria-label={q.label || q.id} rows={4} placeholder="Longitude, latitude — or a GeoJSON geometry"
              value={typeof values[q.id] === 'string' ? String(values[q.id]) : JSON.stringify(values[q.id] ?? '')}
              onChange={(e) => setValues({ ...values, [q.id]: e.target.value })} />
            : q.type === 'text-area' ? <textarea aria-label={q.label || q.id} rows={5} value={String(values[q.id] ?? '')} onChange={(e) => setValues({ ...values, [q.id]: e.target.value })} />
              : <span style={{ display: 'flex', gap: 6 }}>{(q.type === 'file' || q.type === 'folder') && <button type="button" onClick={() => void choosePath(q)}>Browse…</button>}<input aria-label={q.label || q.id} type={q.secret ? 'password' : q.type === 'number' ? 'number' : q.type === 'date' ? 'date' : 'text'} min={q.type === 'number' ? q.minimum : undefined} max={q.type === 'number' ? q.maximum : undefined} value={String(values[q.id] ?? '')}
                onChange={(e) => setValues({ ...values, [q.id]: q.type === 'number' ? (e.target.value === '' ? null : Number(e.target.value)) : e.target.value })} /></span>}
        {q.description && <small style={{ display: 'block' }}>{q.description}</small>}
      </label>)}
      {error && <p role="alert">{error}</p>}
      <div className="actions"><button onClick={() => close(null)}>Cancel</button><button className="primary" onClick={submit}>Run</button></div>
    </div>
  </div>;
}

type TreeChoiceItem = { label: string; value: string | null; children: TreeChoiceItem[] };
function buildTree(paths: unknown[]): TreeChoiceItem[] {
  const roots: TreeChoiceItem[] = [];
  for (const value of paths) {
    if (typeof value !== 'string') continue;
    const parts = value.split('>').map((part) => part.trim());
    let list = roots;
    let path = '';
    parts.forEach((label, index) => {
      path = path ? `${path} > ${label}` : label;
      let item = list.find((candidate) => candidate.label === label);
      if (!item) { item = { label, value: null, children: [] }; list.push(item); }
      if (index === parts.length - 1) item.value = path;
      list = item.children;
    });
  }
  return roots;
}

function TreeChoice({ item, selected, onSelect }: { item: TreeChoiceItem; selected: string; onSelect: (value: string) => void }) {
  if (item.children.length) return <details className="application-tree-branch">
    <summary role="treeitem" aria-expanded={undefined}>{item.label}</summary><div role="group" className="application-tree-group">
      {item.children.map((child) => <TreeChoice key={`${item.label}/${child.label}`} item={child} selected={selected} onSelect={onSelect} />)}
    </div>
  </details>;
  return <button type="button" role="treeitem" aria-pressed={selected === item.value} className="application-tree-leaf"
    onClick={() => item.value && onSelect(item.value)}>{item.label}</button>;
}

/** "lon, lat" as a GeoJSON point; anything else is sent as typed and checked by the engine. */
export function mapAnswer(value: unknown): unknown {
  const m = typeof value === 'string' ? value.trim().match(/^(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$/) : null;
  return m ? { type: 'Point', coordinates: [Number(m[1]), Number(m[2])] } : value;
}

type SavedConnection = { id: string; name?: string; kind?: string };

/** Saved database (Connections panel) and web/GitHub/SMTP connections, filtered by the question's kinds. */
function ConnectionChoice({ question, value, onChange }: { question: AppQuestion; value: string; onChange: (v: string) => void }) {
  const [items, setItems] = useState<SavedConnection[] | null>(null);
  useEffect(() => {
    const kinds = question.kinds?.length ? question.kinds : ['database', 'web', 'github', 'smtp'];
    void Promise.all([
      kinds.includes('database') ? engine().request<SavedConnection[]>('database.connections.list').then((r) => r.map((c) => ({ ...c, kind: 'database' }))) : [],
      engine().request<SavedConnection[]>('credentials.list', {}).then((r) => r.filter((c) => kinds.includes(String(c.kind)))),
    ]).then(([db, other]) => setItems([...db, ...other])).catch(() => setItems([]));
  }, [question.kinds]);
  if (items === null) return <span>Loading connections…</span>;
  if (!items.length) return <span>No saved {question.kinds?.join(' / ') ?? ''} connections. Add one in Settings.</span>;
  return <select aria-label={question.label || question.id} value={value} onChange={(e) => onChange(e.target.value)}>
    <option value="">Choose a connection</option>
    {items.map((c) => <option key={c.id} value={c.id}>{c.name || c.id} ({c.kind})</option>)}
  </select>;
}
