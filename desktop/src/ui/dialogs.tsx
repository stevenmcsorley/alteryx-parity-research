import { useEffect, useRef, useState } from 'react';
import { isTauri } from '../engine/transport';

type Request = { title: string; initial: string; ext: string; mode: 'save' | 'open'; resolve: (v: string | null) => void };
let host: ((r: Request) => void) | null = null;

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

export function DialogHost() {
  const [req, setReq] = useState<Request | null>(null);
  const [value, setValue] = useState('');
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    host = (r) => { setReq(r); setValue(r.initial); };
    return () => { host = null; };
  }, []);
  useEffect(() => { if (req) input.current?.select(); }, [req]);
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
