import { useEffect, useRef, useState } from 'react';
import { AGENT_TOOLS, dispatchAgentTool, type AgentToolName } from './protocol';
import { useEditor } from '../state/store';
import { validateNow } from '../state/engineActions';

type Entry = { label: string; tool: AgentToolName; args?: Record<string, unknown> };
type Model = { slug: string; display_name: string };
type ChatMessage = { role: 'user' | 'assistant'; text: string };
type NativeEvent = { type: 'delta' | 'completed' | 'cancelled'; text?: string; output?: ValueItem[] };
type ValueItem = { type?: string; name?: string; arguments?: string; call_id?: string; [key: string]: unknown };
const initial: Entry[] = [
  { label: 'Inspect active workflow', tool: 'workflow.get_snapshot' },
  { label: 'Check workflow validation', tool: 'workflow.validate' },
  { label: 'Show current diagnostics', tool: 'workflow.get_diagnostics' },
  { label: 'List implemented capabilities', tool: 'app.get_capabilities' },
];

async function native<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke } = await import('@tauri-apps/api/core');
  return invoke<T>(command, args);
}

export function AiSidebar({ close, embedded = false }: { close: () => void; embedded?: boolean }) {
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState('');
  const [connected, setConnected] = useState(false);
  const [authorized, setAuthorized] = useState(false);
  const [sessionRecovery, setSessionRecovery] = useState(false);
  const [account, setAccount] = useState('');
  const [models, setModels] = useState<Model[]>([]);
  const [model, setModel] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [activity, setActivity] = useState<string[]>([]);
  const [routingConsent, setRoutingConsent] = useState(false);
  const [consentUpdating, setConsentUpdating] = useState(false);
  const conversation = useRef<ValueItem[]>([]);
  const currentText = useRef('');
  const stopRequested = useRef(false);
  const name = useEditor((s) => s.name);
  const nodes = useEditor((s) => s.nodes.length);

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let mounted = true;
    void import('@tauri-apps/api/event').then(({ listen }) => listen<NativeEvent>('ai-stream', ({ payload }) => {
      if (!mounted) return;
      if (payload.type === 'delta') {
        currentText.current += payload.text ?? '';
        setMessages((items) => [...items.slice(0, -1), { role: 'assistant', text: currentText.current }]);
      }
    })).then((stop) => { unlisten = stop; }).catch(() => undefined);
    void refreshAuth();
    return () => { mounted = false; unlisten?.(); };
  }, []);

  async function refreshAuth() {
    try {
      const status = await native<{ connected: boolean; plan_usage_authorized: boolean; subject_hint?: string; session_recovery_required?: boolean }>('ai_status');
      setConnected(status.connected); setAuthorized(status.plan_usage_authorized); setAccount(status.subject_hint ?? '');
      setSessionRecovery(Boolean(status.session_recovery_required));
      if (!status.connected) setRoutingConsent(false);
      if (status.connected && status.plan_usage_authorized) await refreshModels();
    } catch { setConnected(false); setAuthorized(false); }
  }
  async function refreshModels() {
    try {
      const choices = await native<Model[]>('ai_models');
      setModels(choices); setModel((selected) => choices.some((x) => x.slug === selected) ? selected : choices[0]?.slug ?? '');
    } catch (e) { setModels([]); setModel(''); setError(messageOf(e)); }
  }
  async function signIn() {
    setBusy(true); setError('');
    try { await native('ai_sign_in'); await refreshAuth(); }
    catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  }
  async function signOut() {
    setBusy(true); setError('');
    try {
      const status = await native<{ remote_revocation_confirmed: boolean }>('ai_sign_out');
      setConnected(false); setAuthorized(false); setAccount(''); setModels([]); setModel(''); setRoutingConsent(false); conversation.current = []; setMessages([]);
      if (!status.remote_revocation_confirmed) setError('Disconnected locally. OpenAI could not confirm remote token revocation; the local protected session has been removed.');
    }
    catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  }
  async function inspect(entry: Entry) {
    setBusy(true); setError(''); setResult(null);
    try {
      const value = await dispatchAgentTool({ getState: () => useEditor.getState(), validate: async () => { await validateNow(); } }, entry.tool, entry.args);
      setResult(value);
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  }
  async function search() {
    const text = query.trim();
    if (!text) return;
    await inspect({ label: 'Search catalogue', tool: 'catalog.search_tools', args: { query: text } });
  }

  async function send() {
    const text = draft.trim();
    if (!text || !model || streaming || !authorized || !routingConsent) return;
    try { await native('ai_begin'); } catch (e) { setError(messageOf(e)); return; }
    setError(''); setDraft(''); setStreaming(true); setActivity([]); stopRequested.current = false;
    conversation.current.push({ role: 'user', content: [{ type: 'input_text', text }] });
    setMessages((items) => [...items, { role: 'user', text }, { role: 'assistant', text: '' }]);
    currentText.current = '';
    const toolDefs = AGENT_TOOLS.map((t) => ({ type: 'function', name: t.name, description: t.description, parameters: t.parameters, strict: true }));
    try {
      for (let turn = 0; turn < 5; turn++) {
        const output = await native('ai_respond', { model, input: conversation.current, tools: toolDefs });
        const completed = output as { output?: ValueItem[] };
        const items = completed.output ?? [];
        conversation.current.push(...items);
        const calls = items.filter((item) => item.type === 'function_call');
        if (!calls.length) break;
        if (turn === 4) throw new Error('The assistant reached the read-only tool-call limit for this turn.');
        for (const call of calls) {
          const toolName = call.name ?? '';
          setActivity((a) => [...a, `Using ${toolName}`]);
          let result: unknown;
          try {
            result = await dispatchAgentTool({ getState: () => useEditor.getState(), validate: async () => { await validateNow(); } }, toolName,
              JSON.parse(call.arguments ?? '{}'));
          } catch (e) { result = { error: messageOf(e) }; }
          let serialized = JSON.stringify(result);
          if (serialized.length > 16_000) serialized = `${serialized.slice(0, 16_000)}… (bounded by Flowbench)`;
          conversation.current.push({ type: 'function_call_output', call_id: call.call_id, output: serialized });
        }
        currentText.current = '';
        setMessages((itemsNow) => [...itemsNow, { role: 'assistant', text: '' }]);
      }
    } catch (e) { if (!stopRequested.current) setError(messageOf(e)); }
    finally { setStreaming(false); }
  }

  async function cancel() {
    stopRequested.current = true;
    try { await native('ai_cancel'); } catch { /* request may already have ended */ }
  }
  async function updateConsent(allowed: boolean) {
    setConsentUpdating(true); setError('');
    try { await native('ai_set_data_consent', { allowed }); setRoutingConsent(allowed); }
    catch (e) { setError(messageOf(e)); }
    finally { setConsentUpdating(false); }
  }

  return <aside className={`${embedded ? 'ai-content' : 'app-right'} ai-sidebar`} aria-label="Flowbench AI workspace" data-testid="ai-sidebar">
    <header className="ai-head">
      <div><strong>Flowbench AI</strong><div className="ai-subtitle">Application agent · v1</div></div>
      <button className="icon" onClick={close} aria-label="Close Flowbench AI">×</button>
    </header>
    <div className="ai-routing"><span className={`routing-dot ${connected ? 'connected' : ''}`} />
      {connected && authorized ? `ChatGPT plan · ${account}` : 'Local application data only'}
      <span className="ai-subtitle"> · External model receives your prompt and only bounded tool results</span></div>
    <section className="ai-context" aria-label="Current application context">
      <div className="ai-subtitle">OPEN WORKFLOW</div><strong>{name}</strong><div>{nodes} node{nodes === 1 ? '' : 's'}</div>
    </section>
    {!connected || !authorized ? <section className="ai-connect">
      <strong>{connected ? 'Plan usage permission required' : 'Connect ChatGPT'}</strong>
      <p>{sessionRecovery ? 'The protected session could not be unlocked. Sign in again to replace it.' : connected ? 'Identity is signed in, but ChatGPT plan inference permission was not granted.' : 'Sign in through OpenAI’s official browser flow. Flowbench stores tokens in Windows user-protected storage.'}</p>
      {busy ? <button onClick={() => void native('ai_cancel').catch(() => undefined)}>Cancel sign-in</button> :
        <button onClick={() => void signIn()}>Continue with ChatGPT</button>}
    </section> : <section className="ai-connect ai-provider">
      <label htmlFor="ai-model">Available model</label>
      <select id="ai-model" value={model} onChange={(e) => setModel(e.target.value)} disabled={streaming || !models.length}>
        {models.map((item) => <option key={item.slug} value={item.slug}>{item.display_name}</option>)}
      </select>
      <button disabled={busy || streaming} onClick={() => void refreshModels()}>Refresh models</button>
      <button disabled={busy || streaming} onClick={() => void signIn()}>Reauthorize</button>
      <button disabled={busy || streaming} onClick={() => void signOut()}>Disconnect</button>
    </section>}
    <section className="ai-actions" aria-label="Local read-only agent actions">
      <div className="ai-subtitle">LOCAL APPLICATION ACTIONS</div>
      {initial.map((entry) => <button key={entry.tool} disabled={busy || streaming} onClick={() => void inspect(entry)}>{entry.label}</button>)}
    </section>
    <form className="ai-search" onSubmit={(event) => { event.preventDefault(); void search(); }}>
      <label htmlFor="ai-catalog-search">Search Flowbench tool catalogue</label>
      <div><input id="ai-catalog-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="e.g. summarize, spatial" />
        <button className="primary" disabled={busy || streaming || !query.trim()}>Search</button></div>
    </form>
    {authorized && <section className="ai-chat" aria-label="ChatGPT conversation">
      <div className="ai-chat-messages" aria-live="polite">{messages.map((message, i) => <article className={`ai-message ${message.role}`} key={i}>
        <strong>{message.role === 'user' ? 'You' : 'Flowbench AI'}</strong><div>{message.text || (streaming ? 'Thinking…' : '')}</div>
      </article>)}</div>
      {activity.length > 0 && <div className="ai-activity" role="status">{activity.slice(-4).map((item, i) => <div key={i}>{item}</div>)}</div>}
      <label className="ai-consent"><input type="checkbox" checked={routingConsent} disabled={consentUpdating || streaming} onChange={(e) => void updateConsent(e.target.checked)} />
        I approve sending my prompt and bounded read-only tool results to ChatGPT for this conversation.</label>
      <form className="ai-compose" onSubmit={(e) => { e.preventDefault(); void send(); }}>
        <textarea aria-label="Message Flowbench AI" value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Ask about this workflow or Flowbench tools…" disabled={streaming} />
        {streaming ? <button type="button" onClick={() => void cancel()}>Stop</button> : <button className="primary" disabled={consentUpdating || !routingConsent || !draft.trim() || !model}>Send</button>}
      </form>
    </section>}
    {busy && <p role="status">Working…</p>}
    {streaming && <p role="status">Receiving streamed response…</p>}
    {error && <div className="ai-error" role="alert">{error}</div>}
    {result !== null && <pre className="ai-result" aria-label="Tool result">{JSON.stringify(result, null, 2)}</pre>}
  </aside>;
}

function messageOf(error: unknown) { return error instanceof Error ? error.message : String(error); }
