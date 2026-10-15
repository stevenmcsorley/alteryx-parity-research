import { useEffect, useRef, useState } from 'react';
import { isTauri } from '../engine/transport';

type Request = { title: string; initial: string; ext: string; mode: 'save' | 'open'; resolve: (v: string | null) => void };
export type AppQuestion = { id: string; label?: string; type: 'text' | 'boolean' | 'number' | 'choice' | 'multi-choice' | 'date' | 'file' | 'folder'; default?: unknown; required?: boolean; options?: unknown[]; description?: string; secret?: boolean; minimum?: number; maximum?: number };
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
        const result = await dlg.open({ title: q.label || q.id, directory: q.type === 'folder', multiple: false });
        selected = typeof result === 'string' ? result : null;
      } else selected = window.prompt(q.type === 'folder' ? 'Choose a folder path' : 'Choose a file path', String(values[q.id] ?? ''));
      if (selected !== null) setValues((old) => ({ ...old, [q.id]: selected }));
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };
  const submit = () => {
    const missing = request.questions.find((q) => q.required && (values[q.id] === '' || values[q.id] == null));
    if (missing) { setError(`${missing.label || missing.id} is required.`); return; }
    const outOfRange = request.questions.find((q) => q.type === 'number' && values[q.id] != null && ((q.minimum != null && Number(values[q.id]) < q.minimum) || (q.maximum != null && Number(values[q.id]) > q.maximum)));
    if (outOfRange) { setError(`${outOfRange.label || outOfRange.id} is outside the allowed range.`); return; }
    close(values);
  };
  return <div className="modal-backdrop" onMouseDown={() => close(null)}>
    <div className="modal" role="dialog" aria-modal="true" aria-label={request.title} onMouseDown={(e) => e.stopPropagation()}>
      <h3>{request.title}</h3>
      {request.questions.map((q) => <label key={q.id} style={{ display: 'block', margin: '12px 0' }}>
        <span>{q.label || q.id}{q.required ? ' *' : ''}</span>
        {q.type === 'boolean' ? <input aria-label={q.label || q.id} type="checkbox" checked={Boolean(values[q.id])}
          onChange={(e) => setValues({ ...values, [q.id]: e.target.checked })} />
          : q.type === 'choice' || q.type === 'multi-choice' ? <select aria-label={q.label || q.id} multiple={q.type === 'multi-choice'} value={q.type === 'multi-choice' ? (values[q.id] as string[] ?? []) : String(values[q.id] ?? '')}
            onChange={(e) => setValues({ ...values, [q.id]: q.type === 'multi-choice' ? [...e.target.selectedOptions].map((o) => o.value) : e.target.value })}>{(q.options ?? []).map((option) => <option key={String(option)} value={String(option)}>{String(option)}</option>)}</select>
            : <span style={{ display: 'flex', gap: 6 }}>{(q.type === 'file' || q.type === 'folder') && <button type="button" onClick={() => void choosePath(q)}>Browse…</button>}<input aria-label={q.label || q.id} type={q.secret ? 'password' : q.type === 'number' ? 'number' : q.type === 'date' ? 'date' : 'text'} min={q.type === 'number' ? q.minimum : undefined} max={q.type === 'number' ? q.maximum : undefined} value={String(values[q.id] ?? '')}
              onChange={(e) => setValues({ ...values, [q.id]: q.type === 'number' ? (e.target.value === '' ? null : Number(e.target.value)) : e.target.value })} /></span>}
        {q.description && <small style={{ display: 'block' }}>{q.description}</small>}
      </label>)}
      {error && <p role="alert">{error}</p>}
      <div className="actions"><button onClick={() => close(null)}>Cancel</button><button className="primary" onClick={submit}>Run</button></div>
    </div>
  </div>;
}
