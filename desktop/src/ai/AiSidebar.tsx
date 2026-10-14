import { useState } from 'react';
import { dispatchAgentTool, type AgentToolName } from './protocol';
import { useEditor } from '../state/store';
import { validateNow } from '../state/engineActions';

type Entry = { label: string; tool: AgentToolName; args?: Record<string, unknown> };
const initial: Entry[] = [
  { label: 'Inspect active workflow', tool: 'workflow.get_snapshot' },
  { label: 'Check workflow validation', tool: 'workflow.validate' },
  { label: 'Show current diagnostics', tool: 'workflow.get_diagnostics' },
  { label: 'List implemented capabilities', tool: 'app.get_capabilities' },
];

export function AiSidebar({ close }: { close: () => void }) {
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState('');
  const name = useEditor((s) => s.name);
  const nodes = useEditor((s) => s.nodes.length);

  async function inspect(entry: Entry) {
    setBusy(true); setError(''); setResult(null);
    try {
      const value = await dispatchAgentTool({ getState: () => useEditor.getState(), validate: async () => { await validateNow(); } }, entry.tool, entry.args);
      setResult(value);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  async function search() {
    const text = query.trim();
    if (!text) return;
    await inspect({ label: 'Search catalogue', tool: 'catalog.search_tools', args: { query: text } });
  }

  return <aside className="app-right ai-sidebar" aria-label="Flowbench AI workspace" data-testid="ai-sidebar">
    <header className="ai-head">
      <div><strong>Flowbench AI</strong><div className="ai-subtitle">Application agent · v1</div></div>
      <button className="icon" onClick={close} aria-label="Close Flowbench AI">×</button>
    </header>
    <div className="ai-routing"><span className="routing-dot" />Local application data only <span className="ai-subtitle">· No model connected</span></div>
    <section className="ai-context" aria-label="Current application context">
      <div className="ai-subtitle">OPEN WORKFLOW</div><strong>{name}</strong><div>{nodes} node{nodes === 1 ? '' : 's'}</div>
    </section>
    <section className="ai-connect">
      <strong>ChatGPT connection is not configured</strong>
      <p>This workspace can inspect the open Flowbench application locally. It does not send workflow data to an AI provider.</p>
      <button disabled title="Sign in with ChatGPT is not implemented yet">Continue with ChatGPT</button>
    </section>
    <section className="ai-actions" aria-label="Local read-only agent actions">
      <div className="ai-subtitle">LOCAL APPLICATION ACTIONS</div>
      {initial.map((entry) => <button key={entry.tool} disabled={busy} onClick={() => void inspect(entry)}>{entry.label}</button>)}
    </section>
    <form className="ai-search" onSubmit={(event) => { event.preventDefault(); void search(); }}>
      <label htmlFor="ai-catalog-search">Search Flowbench tool catalogue</label>
      <div><input id="ai-catalog-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="e.g. summarize, spatial" />
        <button className="primary" disabled={busy || !query.trim()}>Search</button></div>
    </form>
    {busy && <p role="status">Inspecting local application…</p>}
    {error && <div className="ai-error" role="alert">{error}</div>}
    {result !== null && <pre className="ai-result" aria-label="Tool result">{JSON.stringify(result, null, 2)}</pre>}
  </aside>;
}
