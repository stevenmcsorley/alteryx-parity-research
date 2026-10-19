import { useEffect, useState } from 'react';
import { engine, describe } from '../state/engineActions';

type Connection = { id: string; name: string; dialect: 'postgresql' | 'sqlserver' | 'mongodb' | 'odbc'; options?: { driver?: string; dsn?: string; [key: string]: unknown }; host: string; port: number;
  database: string; user: string; tls: string; ca_file?: string | null; connect_timeout: number; query_timeout: number;
  allow_writes: boolean };
const blank = (): Connection => ({ id: '', name: '', dialect: 'postgresql', host: 'localhost', port: 5432,
  database: '', user: '', tls: 'prefer', ca_file: null, connect_timeout: 10, query_timeout: 300, allow_writes: false });

export function DatabaseConnections({ close }: { close: () => void }) {
  const [items, setItems] = useState<Connection[]>([]);
  const [form, setForm] = useState<Connection>(blank());
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const refresh = async () => setItems(await engine().request<Connection[]>('database.connections.list'));
  useEffect(() => { void refresh().catch((e) => setMessage(describe(e))); }, []);
  const set = (key: keyof Connection, value: unknown) => setForm((x) => ({ ...x, [key]: value }));
  const edit = (c: Connection) => { setForm({ ...c }); setPassword(''); setMessage(''); };
  const save = async () => {
    setBusy(true); setMessage('');
    try {
      const connection = { ...form, port: Number(form.port), connect_timeout: Number(form.connect_timeout),
        query_timeout: Number(form.query_timeout), ...(form.id ? {} : { id: undefined }) };
      await engine().request('database.connections.save', { connection, ...(password ? { password } : {}) });
      setPassword(''); await refresh(); setMessage('Connection saved.');
    } catch (e) { setMessage(describe(e)); } finally { setBusy(false); }
  };
  const test = async () => {
    if (!form.id) { setMessage('Save the connection before testing it.'); return; }
    setBusy(true);
    try { await engine().request('database.test', { connection_id: form.id }); setMessage('Connection succeeded.'); }
    catch (e) { setMessage(describe(e)); } finally { setBusy(false); }
  };
  const remove = async (c: Connection) => {
    if (!window.confirm(`Remove connection “${c.name}” and its saved credential?`)) return;
    setBusy(true);
    try { await engine().request('database.connections.delete', { connection_id: c.id });
      if (form.id === c.id) { setForm(blank()); setPassword(''); } await refresh(); setMessage('Connection removed.'); }
    catch (e) { setMessage(describe(e)); } finally { setBusy(false); }
  };
  return <div className="db-modal-backdrop" role="presentation" onMouseDown={(e) => e.target === e.currentTarget && close()}>
    <section className="db-modal" role="dialog" aria-modal="true" aria-labelledby="db-title">
      <header><h2 id="db-title">Database connections</h2><button onClick={close} aria-label="Close">×</button></header>
      <div className="db-modal-body">
        <div className="db-connection-list">
          {items.map((c) => <div key={c.id} className="db-connection-row">
            <button className="db-connection-select" onClick={() => edit(c)}><strong>{c.name}</strong><small>{c.dialect} · {c.host}:{c.port}/{c.database}</small></button>
            <button onClick={() => void remove(c)} disabled={busy} aria-label={`Remove ${c.name}`}>Remove</button>
          </div>)}
          {!items.length && <p className="help">No database connections yet.</p>}
          <button onClick={() => { setForm(blank()); setPassword(''); setMessage(''); }}>New connection</button>
        </div>
        <div className="db-connection-form">
          <label>Name<input value={form.name} onChange={(e) => set('name', e.target.value)} /></label>
          <label>Database type<select value={form.dialect} onChange={(e) => {
            const dialect = e.target.value as Connection['dialect'];
            setForm((f) => ({ ...f, dialect, port: dialect === 'postgresql' ? 5432 : dialect === 'mongodb' ? 27017 : dialect === 'odbc' ? 0 : 1433 }));
          }}><option value="postgresql">PostgreSQL</option><option value="sqlserver">Microsoft SQL Server</option><option value="mongodb">MongoDB</option><option value="odbc">ODBC (any driver)</option></select></label>
          {form.dialect === 'odbc' && <>
            <label>ODBC driver<input aria-label="ODBC driver" placeholder="PostgreSQL Unicode, SQLite3, ..." value={form.options?.driver ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, options: { ...f.options, driver: e.target.value || undefined } }))} /></label>
            <label>Or DSN<input aria-label="ODBC DSN" value={form.options?.dsn ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, options: { ...f.options, dsn: e.target.value || undefined } }))} /></label>
            <p className="help">Host, port, database and user are passed to the driver when set; the password goes to the credential vault.</p>
          </>}
          <label>Host<input value={form.host} onChange={(e) => set('host', e.target.value)} /></label>
          <label>Port<input type="number" min="1" max="65535" value={form.port} onChange={(e) => set('port', e.target.value)} /></label>
          <label>Database<input value={form.database} onChange={(e) => set('database', e.target.value)} /></label>
          <label>User<input autoComplete="username" value={form.user} onChange={(e) => set('user', e.target.value)} /></label>
          <label>Password<input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder={form.id ? 'Leave blank to keep saved credential' : ''} /></label>
          <label>TLS<select value={form.tls} onChange={(e) => set('tls', e.target.value)}>
            <option value="disable">Disable</option><option value="prefer">Prefer</option><option value="require">Require</option>
            {form.dialect !== 'sqlserver' && <option value="verify-full">Verify certificate and host</option>}
          </select></label>
          {form.dialect !== 'sqlserver' && <label>CA certificate path<input value={form.ca_file ?? ''} onChange={(e) => set('ca_file', e.target.value || null)} /></label>}
          <label>Connection timeout (seconds)<input type="number" min="1" value={form.connect_timeout} onChange={(e) => set('connect_timeout', e.target.value)} /></label>
          <label>Query timeout (seconds; 0 = none)<input type="number" min="0" value={form.query_timeout} onChange={(e) => set('query_timeout', e.target.value)} /></label>
          <label className="db-check"><input type="checkbox" checked={form.allow_writes} onChange={(e) => set('allow_writes', e.target.checked)} />Allow database writes on this connection</label>
          <div className="db-actions"><button onClick={() => void save()} disabled={busy || !form.name || !form.host || !form.database || !form.user}>{busy ? 'Working…' : 'Save'}</button>
            <button onClick={() => void test()} disabled={busy || !form.id}>Test</button></div>
          {message && <div role="status" className="db-message">{message}</div>}
          <p className="help">Credentials are stored in the operating system credential vault. Connections default to read-only; each database write also needs a per-run grant.</p>
        </div>
      </div>
    </section>
  </div>;
}
