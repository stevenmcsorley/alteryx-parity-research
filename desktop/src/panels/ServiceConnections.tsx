import { useEffect, useState } from 'react';
import { engine, describe } from '../state/engineActions';

export type ServiceConnection = { id: string; name: string; kind: 'web' | 'github' | 'smtp'; base_url: string;
  auth: 'bearer' | 'basic' | 'header'; username?: string | null; header_name?: string | null };
const blank = (): ServiceConnection => ({ id: '', name: '', kind: 'web', base_url: 'https://', auth: 'bearer', username: null, header_name: null });

export function ServiceConnections({ close }: { close: () => void }) {
  const [items, setItems] = useState<ServiceConnection[]>([]);
  const [form, setForm] = useState<ServiceConnection>(blank());
  const [secret, setSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const refresh = async () => setItems(await engine().request<ServiceConnection[]>('credentials.list'));
  useEffect(() => { void refresh().catch((e) => setMessage(describe(e))); }, []);
  const set = (patch: Partial<ServiceConnection>) => setForm((x) => ({ ...x, ...patch }));
  const edit = (c: ServiceConnection) => { setForm({ ...c }); setSecret(''); setMessage(''); };
  const save = async () => {
    setBusy(true); setMessage('');
    try {
      const connection = { ...form, ...(form.id ? {} : { id: undefined }),
        username: form.auth === 'basic' ? form.username : null, header_name: form.auth === 'header' ? form.header_name : null };
      await engine().request('credentials.save', { connection, ...(secret ? { secret } : {}) });
      setSecret(''); await refresh(); setMessage('Connection saved.');
    } catch (e) { setMessage(describe(e)); } finally { setBusy(false); }
  };
  const remove = async (c: ServiceConnection) => {
    if (!window.confirm(`Remove connection “${c.name}” and its saved credential?`)) return;
    setBusy(true);
    try { await engine().request('credentials.delete', { connection_id: c.id });
      if (form.id === c.id) { setForm(blank()); setSecret(''); } await refresh(); setMessage('Connection removed.'); }
    catch (e) { setMessage(describe(e)); } finally { setBusy(false); }
  };
  const kindLabel = (k: ServiceConnection['kind']) => k === 'github' ? 'GitHub' : k === 'smtp' ? 'Email (SMTP)' : 'Web/API';
  const secretLabel = form.auth === 'basic' ? 'Password' : form.kind === 'github' ? 'Personal access token' : form.auth === 'header' ? 'Header value (API key)' : 'Token';
  return <div className="db-modal-backdrop" role="presentation" onMouseDown={(e) => e.target === e.currentTarget && close()}>
    <section className="db-modal" role="dialog" aria-modal="true" aria-labelledby="svc-title">
      <header><h2 id="svc-title">Web &amp; API connections</h2><button onClick={close} aria-label="Close">×</button></header>
      <div className="db-modal-body">
        <div className="db-connection-list">
          {items.map((c) => <div key={c.id} className="db-connection-row">
            <button className="db-connection-select" onClick={() => edit(c)}><strong>{c.name}</strong><small>{kindLabel(c.kind)} · {c.base_url}</small></button>
            <button onClick={() => void remove(c)} disabled={busy} aria-label={`Remove ${c.name}`}>Remove</button>
          </div>)}
          {!items.length && <p className="help">No web, GitHub or email connections yet.</p>}
          <button onClick={() => { setForm(blank()); setSecret(''); setMessage(''); }}>New connection</button>
        </div>
        <div className="db-connection-form">
          <label>Name<input value={form.name} onChange={(e) => set({ name: e.target.value })} /></label>
          <label>Type<select aria-label="Connection type" value={form.kind} disabled={!!form.id} onChange={(e) => {
            const kind = e.target.value as ServiceConnection['kind'];
            set(kind === 'github' ? { kind, auth: 'bearer', base_url: 'https://api.github.com' }
              : kind === 'smtp' ? { kind, auth: 'basic', base_url: 'smtps://smtp.example.com:465' } : { kind, base_url: 'https://' });
          }}><option value="web">Web/API</option><option value="github">GitHub</option><option value="smtp">Email (SMTP)</option></select></label>
          <label>{form.kind === 'smtp' ? 'SMTP server' : 'Base URL'}<input aria-label="Base URL" value={form.base_url} onChange={(e) => set({ base_url: e.target.value })} /></label>
          {form.kind === 'smtp' && <p className="help">smtps://host:465 encrypts from the start; smtp://host:587 uses STARTTLS.</p>}
          {form.kind === 'web' && <label>Authentication<select aria-label="Authentication" value={form.auth} onChange={(e) => set({ auth: e.target.value as ServiceConnection['auth'] })}>
            <option value="bearer">Bearer token</option><option value="basic">Username and password</option><option value="header">API key header</option>
          </select></label>}
          {form.auth === 'basic' && <label>Username<input autoComplete="username" value={form.username ?? ''} onChange={(e) => set({ username: e.target.value })} /></label>}
          {form.auth === 'header' && <label>Header name<input aria-label="Header name" placeholder="X-Api-Key" value={form.header_name ?? ''} onChange={(e) => set({ header_name: e.target.value })} /></label>}
          <label>{secretLabel}<input aria-label="Secret" type="password" autoComplete="new-password" value={secret} onChange={(e) => setSecret(e.target.value)}
            placeholder={form.id ? 'Leave blank to keep saved credential' : ''} /></label>
          <div className="db-actions"><button onClick={() => void save()} disabled={busy || !form.name || !form.base_url || (!form.id && !secret)}>{busy ? 'Working…' : 'Save'}</button></div>
          {message && <div role="status" className="db-message">{message}</div>}
          <p className="help">{form.kind === 'smtp'
            ? 'The password is stored in the operating system credential vault and is only sent to this mail server over an encrypted connection. Workflows store only the connection, never the password.'
            : 'The credential is stored in the operating system credential vault and is only sent to URLs under the base URL, including after redirects. Workflows store only the connection, never the secret.'}</p>
        </div>
      </div>
    </section>
  </div>;
}
