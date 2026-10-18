import { useEffect, useState } from 'react';
import { engine, describe } from '../state/engineActions';

export type ServiceConnection = { id: string; name: string; kind: 'web' | 'github' | 'smtp'; base_url: string;
  auth: 'bearer' | 'basic' | 'header' | 'none' | 'aws-sigv4' | 'oauth2'; username?: string | null; header_name?: string | null;
  token_url?: string | null; client_id?: string | null; scope?: string | null; device_code_url?: string | null };
const blank = (): ServiceConnection => ({ id: '', name: '', kind: 'web', base_url: 'https://', auth: 'bearer', username: null, header_name: null,
  token_url: null, client_id: null, scope: null, device_code_url: null });
type DeviceSignIn = { device_code: string; user_code: string; verification_uri: string; interval: number; expires_in: number; message?: string };

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
        username: form.auth === 'basic' || form.auth === 'aws-sigv4' ? form.username : null, header_name: form.auth === 'header' ? form.header_name : null,
        token_url: form.kind === 'web' && (form.auth === 'bearer' || form.auth === 'oauth2') && form.token_url ? form.token_url : null,
        client_id: form.auth === 'oauth2' ? form.client_id : null, scope: form.auth === 'oauth2' ? form.scope || null : null,
        device_code_url: form.auth === 'oauth2' ? form.device_code_url || null : null };
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
  const [signIn, setSignIn] = useState<DeviceSignIn | null>(null);
  useEffect(() => {
    // Poll the token endpoint (through the engine) until the user finishes signing in in the browser.
    if (!signIn || !form.id) return;
    let stop = false;
    const deadline = Date.now() + signIn.expires_in * 1000;
    let wait = signIn.interval * 1000;
    const poll = async () => {
      while (!stop && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, wait));
        if (stop) return;
        try {
          const r = await engine().request<{ status: string }>('credentials.oauth.device_poll', { connection_id: form.id, device_code: signIn.device_code });
          if (r.status === 'signed_in') { setSignIn(null); setMessage('Signed in. The refresh token is saved in the credential vault.'); return; }
          if (r.status === 'slow_down') wait += 5000;
          if (r.status === 'declined' || r.status === 'expired') { setSignIn(null); setMessage(`Sign-in ${r.status}.`); return; }
        } catch (e) { setSignIn(null); setMessage(describe(e)); return; }
      }
      if (!stop) { setSignIn(null); setMessage('Sign-in timed out.'); }
    };
    void poll();
    return () => { stop = true; };
  }, [signIn, form.id]);
  const startSignIn = async () => {
    setMessage('');
    try { setSignIn(await engine().request<DeviceSignIn>('credentials.oauth.device_start', { connection_id: form.id })); }
    catch (e) { setMessage(describe(e)); }
  };
  const kindLabel = (k: ServiceConnection['kind']) => k === 'github' ? 'GitHub' : k === 'smtp' ? 'Email (SMTP)' : 'Web/API';
  const secretLabel = form.auth === 'oauth2' ? 'Client secret (only for app-only access)' : form.auth === 'aws-sigv4' ? 'Secret access key' : form.auth === 'basic' ? 'Password' : form.kind === 'github' ? 'Personal access token' : form.auth === 'header' ? 'Header value (API key)' : 'Token';
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
            <option value="bearer">Bearer token</option><option value="basic">Username and password</option><option value="header">API key header</option><option value="none">None (open service)</option><option value="oauth2">OAuth 2.0</option><option value="aws-sigv4">AWS Signature V4 (S3)</option>
          </select></label>}
          {(form.auth === 'basic' || form.auth === 'aws-sigv4') && <label>{form.auth === 'aws-sigv4' ? 'Access key id' : 'Username'}<input autoComplete="username" value={form.username ?? ''} onChange={(e) => set({ username: e.target.value })} /></label>}
          {form.auth === 'oauth2' && <>
            <label>Token URL<input aria-label="OAuth token URL" placeholder="https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token" value={form.token_url ?? ''} onChange={(e) => set({ token_url: e.target.value || null })} /></label>
            <label>Client id<input aria-label="OAuth client id" value={form.client_id ?? ''} onChange={(e) => set({ client_id: e.target.value || null })} /></label>
            <label>Scope<input aria-label="OAuth scope" placeholder="Files.ReadWrite offline_access" value={form.scope ?? ''} onChange={(e) => set({ scope: e.target.value || null })} /></label>
            <label>Device sign-in URL (optional)<input aria-label="Device sign-in URL" placeholder="https://login.microsoftonline.com/<tenant>/oauth2/v2.0/devicecode" value={form.device_code_url ?? ''} onChange={(e) => set({ device_code_url: e.target.value || null })} /></label>
          </>}
          {form.auth === 'header' && <label>Header name<input aria-label="Header name" placeholder="X-Api-Key" value={form.header_name ?? ''} onChange={(e) => set({ header_name: e.target.value })} /></label>}
          {form.kind === 'web' && form.auth === 'bearer' && <label>Token URL (optional)<input aria-label="Token URL" placeholder="Only for a refresh token, e.g. a portal's Authentication URL"
            value={form.token_url ?? ''} onChange={(e) => set({ token_url: e.target.value || null })} /></label>}
          {form.auth !== 'none' && <label>{secretLabel}<input aria-label="Secret" type="password" autoComplete="new-password" value={secret} onChange={(e) => setSecret(e.target.value)}
            placeholder={form.id ? 'Leave blank to keep saved credential' : ''} /></label>}
          <div className="db-actions"><button onClick={() => void save()} disabled={busy || !form.name || !form.base_url || (!form.id && !secret && form.auth !== 'none' && form.auth !== 'oauth2')}>{busy ? 'Working…' : 'Save'}</button>
            {form.auth === 'oauth2' && form.device_code_url && <button onClick={() => void startSignIn()} disabled={!form.id || !!signIn}
              title={form.id ? 'Sign in with a code in your browser' : 'Save the connection first'}>Sign in…</button>}</div>
          {signIn && <div className="db-message" role="status" aria-label="Device sign-in">
            Open <a href={signIn.verification_uri} target="_blank" rel="noreferrer noopener">{signIn.verification_uri}</a> and enter the code <b className="oauth-code">{signIn.user_code}</b>. Waiting for you to finish…
            <button onClick={() => setSignIn(null)}>Cancel</button></div>}
          {message && <div role="status" className="db-message">{message}</div>}
          <p className="help">{form.kind === 'smtp'
            ? 'The password is stored in the operating system credential vault and is only sent to this mail server over an encrypted connection. Workflows store only the connection, never the password.'
            : 'The credential is stored in the operating system credential vault and is only sent to URLs under the base URL, including after redirects. Workflows store only the connection, never the secret.'}</p>
        </div>
      </div>
    </section>
  </div>;
}
