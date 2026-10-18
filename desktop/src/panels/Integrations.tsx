import { useEffect, useMemo, useState } from 'react';
import { engine, describe } from '../state/engineActions';
import { DatabaseConnections } from './DatabaseConnections';
import { ServiceConnections } from './ServiceConnections';

type Check = { need: string; ok: boolean; detail: string };
export type ProviderStatus = { id: string; family: string; name: string; kind: 'local-runtime' | 'open-source' | 'vendor';
  serves: string[]; licence?: string | null; homepage?: string | null; setup: string; verification: string;
  semantics: Record<string, unknown>; ready: boolean; checks: Check[] };
type Entitlement = { id: string; product: string; note: string; declared_at: number; validated: boolean };
type DataPack = { id: string; family: string; name: string; path: string; licence: string; source: string; bytes: number; sha256: string };
type Availability = { catalog_id: string; available: boolean; providers: { provider: string; name: string; kind: string; ready: boolean; missing: string[] }[] };
type Runtimes = { python_environments: { name: string; python: string }[]; rscript: string | null; java: string | null };

const SECTIONS = ['Connections', 'Licences', 'Providers', 'Data packs & runtimes', 'Tool availability'] as const;
type Section = typeof SECTIONS[number];

function bytes(n: number) {
  return n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : n >= 1e3 ? `${(n / 1e3).toFixed(1)} kB` : `${n} B`;
}

/** Settings > Integrations & Licences: everything a tool may need from outside Flowbench, in one place. */
export function Integrations({ close, initial = 'Connections' }: { close: () => void; initial?: Section }) {
  const [section, setSection] = useState<Section>(initial);
  const [message, setMessage] = useState('');
  return <div className="db-modal-backdrop" role="presentation" onMouseDown={(e) => e.target === e.currentTarget && close()}>
    <section className="db-modal integrations-modal" role="dialog" aria-modal="true" aria-labelledby="int-title">
      <header><h2 id="int-title">Integrations &amp; Licences</h2><button onClick={close} aria-label="Close">×</button></header>
      <nav className="integrations-tabs" role="tablist">
        {SECTIONS.map((s) => <button key={s} role="tab" aria-selected={section === s} className={section === s ? 'active' : ''}
          onClick={() => { setSection(s); setMessage(''); }}>{s}</button>)}
      </nav>
      <div className="integrations-body" role="tabpanel" aria-label={section}>
        {section === 'Connections' && <ConnectionsSection />}
        {section === 'Licences' && <LicencesSection onMessage={setMessage} />}
        {section === 'Providers' && <ProvidersSection onMessage={setMessage} />}
        {section === 'Data packs & runtimes' && <DataPacksSection onMessage={setMessage} />}
        {section === 'Tool availability' && <AvailabilitySection onMessage={setMessage} />}
      </div>
      {message && <p className="db-message" role="status">{message}</p>}
    </section>
  </div>;
}

function ConnectionsSection() {
  const [open, setOpen] = useState<'db' | 'svc' | null>(null);
  return <div>
    <p className="help">Credentials are kept in your operating system's credential vault. Workflows refer to a connection by its id, never by its secret.</p>
    <div className="integrations-cards">
      <div className="integrations-card"><h3>Databases</h3><p>PostgreSQL and SQL Server connections for the database tools.</p>
        <button onClick={() => setOpen('db')}>Manage database connections</button></div>
      <div className="integrations-card"><h3>Web, API, GitHub and email</h3><p>Base-URL-scoped tokens and passwords for web tools, vendor APIs and SMTP.</p>
        <button onClick={() => setOpen('svc')}>Manage web &amp; API connections</button></div>
    </div>
    {open === 'db' && <DatabaseConnections close={() => setOpen(null)} />}
    {open === 'svc' && <ServiceConnections close={() => setOpen(null)} />}
  </div>;
}

function LicencesSection({ onMessage }: { onMessage: (m: string) => void }) {
  const [items, setItems] = useState<Entitlement[]>([]);
  const [product, setProduct] = useState('');
  const [note, setNote] = useState('');
  const refresh = () => engine().request<Entitlement[]>('integrations.entitlements.list').then(setItems).catch((e) => onMessage(describe(e)));
  useEffect(() => { void refresh(); }, []);
  const declare = async () => {
    try { await engine().request('integrations.entitlements.declare', { product, note }); setProduct(''); setNote(''); await refresh(); onMessage('Licence recorded.'); }
    catch (e) { onMessage(describe(e)); }
  };
  const remove = async (e: Entitlement) => {
    try { await engine().request('integrations.entitlements.remove', { id: e.id }); await refresh(); } catch (err) { onMessage(describe(err)); }
  };
  return <div>
    <p className="help">Record the vendor licences you hold so tools that need them become available. Flowbench takes your word for it: a declared licence is never checked with the vendor, and the vendor's own sign-in still applies when a tool connects.</p>
    <table className="integrations-table"><thead><tr><th>Product</th><th>Note</th><th>Status</th><th /></tr></thead><tbody>
      {items.map((e) => <tr key={e.id}><td>{e.product}</td><td>{e.note}</td><td><span className="badge">declared, not validated</span></td>
        <td><button onClick={() => void remove(e)} aria-label={`Remove ${e.product}`}>Remove</button></td></tr>)}
      {!items.length && <tr><td colSpan={4} className="muted">No licences recorded.</td></tr>}
    </tbody></table>
    <div className="integrations-form">
      <input aria-label="Licensed product" placeholder="Product, for example Alteryx Server" value={product} onChange={(e) => setProduct(e.target.value)} />
      <input aria-label="Licence note" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
      <button className="primary" disabled={!product.trim()} onClick={() => void declare()}>Record licence</button>
    </div>
  </div>;
}

function ProvidersSection({ onMessage }: { onMessage: (m: string) => void }) {
  const [items, setItems] = useState<ProviderStatus[] | null>(null);
  useEffect(() => { void engine().request<ProviderStatus[]>('integrations.providers').then(setItems).catch((e) => onMessage(describe(e))); }, []);
  if (!items) return <p>Checking providers…</p>;
  const kinds: [ProviderStatus['kind'], string][] = [['local-runtime', 'Local runtimes'], ['open-source', 'Open-source services'], ['vendor', 'Vendor services']];
  return <div>
    <p className="help">Tools that delegate to a runtime or service name their provider explicitly; Flowbench never switches provider for you, because results from different providers can mean different things.</p>
    {kinds.map(([kind, title]) => {
      const group = items.filter((p) => p.kind === kind);
      return group.length ? <div key={kind}><h3>{title}</h3>{group.map((p) => <details key={p.id} className="integrations-provider">
        <summary><span className={`status-dot ${p.ready ? 'ok' : 'missing'}`} aria-hidden="true" />
          <b>{p.name}</b> <span className="muted">{p.licence ?? ''}</span> <span className="badge">{p.ready ? 'ready' : 'needs setup'}</span></summary>
        <ul>{p.checks.map((c) => <li key={c.need}>{c.ok ? '✓' : '✗'} {c.detail}</li>)}</ul>
        {!p.ready && p.setup && <p><b>Set up:</b> {p.setup}</p>}
        <p><b>Serves:</b> {p.serves.join(', ')}</p>
        <p><b>Results:</b> {Object.entries(p.semantics).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : String(v)}`).join('; ')}</p>
        <p><b>Verified:</b> {p.verification}</p>
      </details>)}</div> : null;
    })}
  </div>;
}

function DataPacksSection({ onMessage }: { onMessage: (m: string) => void }) {
  const [packs, setPacks] = useState<DataPack[]>([]);
  const [runtimes, setRuntimes] = useState<Runtimes | null>(null);
  const [form, setForm] = useState({ family: '', path: '', licence: '', name: '', source: '' });
  const [busy, setBusy] = useState(false);
  const refresh = () => engine().request<DataPack[]>('integrations.datapacks.list').then(setPacks).catch((e) => onMessage(describe(e)));
  useEffect(() => {
    void refresh();
    void engine().request<Runtimes>('integrations.runtimes').then(setRuntimes).catch((e) => onMessage(describe(e)));
  }, []);
  const register = async () => {
    setBusy(true);
    try { await engine().request('integrations.datapacks.register', form); setForm({ family: '', path: '', licence: '', name: '', source: '' }); await refresh(); onMessage('Data pack registered.'); }
    catch (e) { onMessage(describe(e)); } finally { setBusy(false); }
  };
  return <div>
    <h3>Data packs</h3>
    <p className="help">Local datasets a provider reads, such as a map extract for routing. Flowbench records each pack's licence and checksum and never redistributes it.</p>
    <table className="integrations-table"><thead><tr><th>Family</th><th>Name</th><th>Licence</th><th>Size</th><th>Checksum</th><th /></tr></thead><tbody>
      {packs.map((p) => <tr key={p.id}><td>{p.family}</td><td title={p.path}>{p.name}</td><td>{p.licence}</td><td>{bytes(p.bytes)}</td><td><code>{p.sha256.slice(0, 12)}</code></td>
        <td><button onClick={() => void engine().request('integrations.datapacks.remove', { id: p.id }).then(refresh)} aria-label={`Remove ${p.name}`}>Remove</button></td></tr>)}
      {!packs.length && <tr><td colSpan={6} className="muted">No data packs registered.</td></tr>}
    </tbody></table>
    <div className="integrations-form">
      <input aria-label="Data pack family" placeholder="Family, e.g. osm-extract" value={form.family} onChange={(e) => setForm({ ...form, family: e.target.value })} />
      <input aria-label="Data pack path" placeholder="File or folder path" value={form.path} onChange={(e) => setForm({ ...form, path: e.target.value })} />
      <input aria-label="Data pack licence" placeholder="Licence, e.g. ODbL-1.0" value={form.licence} onChange={(e) => setForm({ ...form, licence: e.target.value })} />
      <input aria-label="Data pack source" placeholder="Source (optional)" value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} />
      <button className="primary" disabled={busy || !form.family || !form.path || !form.licence} onClick={() => void register()}>{busy ? 'Checking…' : 'Register'}</button>
    </div>
    <h3>Runtimes</h3>
    {!runtimes ? <p>Checking…</p> : <ul className="integrations-runtimes">
      <li>Python: {runtimes.python_environments.map((e) => `${e.name} (${e.python})`).join(', ')}</li>
      <li>R: {runtimes.rscript ?? 'not found (install R or set FLOWENGINE_RSCRIPT)'}</li>
      <li>Java: {runtimes.java ?? 'not found (needed for Apache Spark)'}</li>
    </ul>}
  </div>;
}

function AvailabilitySection({ onMessage }: { onMessage: (m: string) => void }) {
  const [rows, setRows] = useState<Availability[] | null>(null);
  const [q, setQ] = useState('');
  useEffect(() => { void engine().request<Availability[]>('integrations.availability').then(setRows).catch((e) => onMessage(describe(e))); }, []);
  const shown = useMemo(() => (rows ?? []).filter((r) => !q.trim() || r.catalog_id.includes(q.trim().toLowerCase())), [rows, q]);
  if (!rows) return <p>Checking tools…</p>;
  return <div>
    <p className="help">Tools that need a runtime, service or licence, and what each still needs before it can run here.</p>
    <input type="search" aria-label="Filter tools" placeholder="Filter tools…" value={q} onChange={(e) => setQ(e.target.value)} />
    <table className="integrations-table"><thead><tr><th>Tool</th><th>Status</th><th>Providers</th></tr></thead><tbody>
      {shown.map((r) => <tr key={r.catalog_id}><td>{r.catalog_id}</td><td><span className={`badge ${r.available ? 'ok' : 'unavailable'}`}>{r.available ? 'available' : 'needs setup'}</span></td>
        <td>{r.providers.map((p) => <div key={p.provider}>{p.name}{p.ready ? ' ✓' : `: ${p.missing.join('; ')}`}</div>)}</td></tr>)}
    </tbody></table>
  </div>;
}
