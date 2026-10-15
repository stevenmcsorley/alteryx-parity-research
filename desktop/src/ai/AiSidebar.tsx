import { useEffect, useRef, useState } from 'react';
import { dispatchAgentTool, type AgentToolName } from './protocol';
import { useEditor } from '../state/store';
import { validateNow } from '../state/engineActions';
import { ensureAiStreamListener, useAiConversation, type ChatMessage } from './conversation';

type Entry = { label: string; tool: AgentToolName; args?: Record<string, unknown> };
type Model = { slug: string; display_name: string };
type ValueItem = { type?: string; name?: string; arguments?: string; call_id?: string; [key: string]: unknown };
const quickActions: Entry[] = [
  { label: 'Inspect this workflow', tool: 'workflow.get_snapshot' },
  { label: 'Check validation', tool: 'workflow.validate' },
  { label: 'Read diagnostics', tool: 'workflow.get_diagnostics' },
  { label: 'List implemented tools', tool: 'app.get_capabilities' },
];

async function native<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke } = await import('@tauri-apps/api/core');
  return invoke<T>(command, args);
}

export function AiSidebar({ close, embedded = false }: { close: () => void; embedded?: boolean }) {
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<unknown>(null);
  const [connected, setConnected] = useState(false);
  const [authorized, setAuthorized] = useState(false);
  const [sessionRecovery, setSessionRecovery] = useState(false);
  const [account, setAccount] = useState('');
  const [models, setModels] = useState<Model[]>([]);
  const [model, setModel] = useState('');
  const [draft, setDraft] = useState('');
  const [routingConsent, setRoutingConsent] = useState(false);
  const [toolsEnabled, setToolsEnabled] = useState(true);
  const [consentUpdating, setConsentUpdating] = useState(false);
  const [error, setError] = useState('');
  const [responseDiagnostics, setResponseDiagnostics] = useState<unknown[]>([]);
  const messages = useAiConversation((s) => s.messages);
  const status = useAiConversation((s) => s.status);
  const chatScroll = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const cancelled = useRef(false);
  const name = useEditor((s) => s.name);
  const nodes = useEditor((s) => s.nodes.length);
  const streaming = ['submitting', 'streaming', 'tool-running'].includes(status);

  useEffect(() => {
    // The app-wide listener remains installed when this panel unmounts or the user switches tabs.
    void ensureAiStreamListener().catch(() => setError('Could not attach to the native response stream. Reopen the AI workspace and retry.'));
    void refreshAuth();
  }, []);
  useEffect(() => {
    const pane = chatScroll.current;
    if (pane && stickToBottom.current) pane.scrollTop = pane.scrollHeight;
  }, [messages, status]);

  async function refreshAuth() {
    try {
      const auth = await native<{ connected: boolean; plan_usage_authorized: boolean; subject_hint?: string; session_recovery_required?: boolean; data_routing_allowed?: boolean }>('ai_status');
      setConnected(auth.connected); setAuthorized(auth.plan_usage_authorized); setAccount(auth.subject_hint ?? '');
      setSessionRecovery(Boolean(auth.session_recovery_required));
      setRoutingConsent(Boolean(auth.data_routing_allowed));
      if (!auth.connected) setRoutingConsent(false);
      if (auth.connected && auth.plan_usage_authorized) await refreshModels();
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
      const auth = await native<{ remote_revocation_confirmed: boolean }>('ai_sign_out');
      setConnected(false); setAuthorized(false); setAccount(''); setModels([]); setModel(''); setRoutingConsent(false);
      useAiConversation.getState().reset();
      if (!auth.remote_revocation_confirmed) setError('Disconnected locally. OpenAI could not confirm remote token revocation; the protected local session was removed.');
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  }
  async function updateConsent(allowed: boolean) {
    setConsentUpdating(true); setError('');
    try { await native('ai_set_data_consent', { allowed }); setRoutingConsent(allowed); }
    catch (e) { setError(messageOf(e)); }
    finally { setConsentUpdating(false); }
  }
  async function inspect(entry: Entry) {
    setBusy(true); setError(''); setResult(null);
    try {
      const value = await dispatchAgentTool({ getState: () => useEditor.getState(), validate: async () => { await validateNow(); } }, entry.tool, entry.args);
      setResult(value);
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  }
  async function newConversation() {
    setError('');
    try { await native('ai_new_conversation'); useAiConversation.getState().reset(); setResult(null); }
    catch (e) { setError(messageOf(e)); }
  }
  async function loadResponseDiagnostics() {
    try {
      const entries = await native<unknown[]>('ai_get_response_diagnostics');
      setResponseDiagnostics(entries);
      if (!entries.length) setError('No native ChatGPT response diagnostics have been recorded in this session.');
    } catch (e) { setError(messageOf(e)); }
  }
  async function send(textOverride?: string) {
    const text = (textOverride ?? draft).trim();
    if (!text || !model || streaming || !authorized || !routingConsent) return;
    stickToBottom.current = true;
    setError(''); setDraft(''); cancelled.current = false;
    let turnId: string | undefined;
    try {
      await ensureAiStreamListener();
      const started = await native<{ turn_id: string; assistant_message_id: string }>('ai_begin', { prompt: text });
      turnId = started.turn_id;
      useAiConversation.getState().begin(
        { id: `user_${turnId}`, turnId, role: 'user', text, state: 'completed' },
        { id: started.assistant_message_id, turnId, role: 'assistant', text: '', state: 'streaming' },
      );
      for (let cycle = 0; cycle < 5; cycle++) {
        const cycleId = `cycle_${crypto.randomUUID().replaceAll('-', '')}`;
        useAiConversation.getState().responseStart(turnId, cycleId);
        const response: { turn_id: string; message_id: string; cycle_id: string; text: string; text_source?: string; empty_reason?: string; calls?: ValueItem[] } = await native('ai_respond', { model, turnId, cycleId, toolsEnabled });
        if (response.turn_id !== turnId || response.message_id !== started.assistant_message_id || response.cycle_id !== cycleId) throw new Error('The response belonged to a different conversation turn.');
        // Completed output is authoritative. This recovers text if event delivery was late or lost.
        useAiConversation.getState().authoritative(turnId, response.message_id, cycleId, response.text ?? '');
        const calls = response.calls ?? [];
        if (!calls.length) {
          if (!response.text?.trim()) throw new Error(emptyResponseMessage(response.empty_reason));
          await native('ai_finish_turn', { turnId });
          useAiConversation.getState().finish(turnId, 'completed');
          return;
        }
        if (cycle === 4) throw new Error('The assistant reached the read-only tool-call limit for this turn.');
        for (const call of calls) {
          const toolName = call.name ?? '';
          useAiConversation.getState().activity(turnId, activityLabel(toolName));
          let toolResult: unknown;
          try {
            toolResult = await dispatchAgentTool({ getState: () => useEditor.getState(), validate: async () => { await validateNow(); } }, toolName,
              JSON.parse(call.arguments ?? '{}'));
          } catch { toolResult = { error: 'The requested read-only tool could not complete.' }; }
          await native('ai_submit_tool_result', { callId: call.call_id, result: toolResult });
        }
      }
    } catch (e) {
      const message = messageOf(e);
      if (turnId) {
        await native('ai_abort_turn', { turnId }).catch(() => undefined);
        useAiConversation.getState().finish(turnId, cancelled.current ? 'cancelled' : 'failed', cancelled.current ? undefined : message);
      } else if (!cancelled.current) {
        setDraft(text);
        setError(message);
      }
    }
  }
  async function cancel() {
    cancelled.current = true;
    try { await native('ai_cancel'); } catch { /* request may already have ended */ }
  }
  async function retry(message: ChatMessage) {
    const s = useAiConversation.getState();
    const index = s.messages.findIndex((m) => m.id === message.id);
    const userMessage = s.messages.slice(0, index).reverse().find((m) => m.role === 'user');
    const prompt = userMessage?.text;
    if (prompt) {
      // Remove the failed turn from the visible transcript before repeating it natively.
      const userIndex = s.messages.findIndex((m) => m.id === userMessage.id);
      useAiConversation.setState({ messages: s.messages.slice(0, userIndex), status: 'idle', turnId: null, assistantMessageId: null, cycleId: null, roundPrefix: '' });
      setDraft(prompt);
      await send(prompt);
    }
  }
  async function localCommand(entry: Entry) { await inspect(entry); }

  return <aside className={`${embedded ? 'ai-content' : 'app-right'} ai-sidebar`} aria-label="Flowbench AI workspace" data-testid="ai-sidebar">
    <header className="ai-head">
      <div className="ai-brand"><span className={`routing-dot ${connected && authorized ? 'connected' : ''}`} title={connected && authorized ? 'ChatGPT connected' : 'ChatGPT not connected'} /><strong>Flowbench AI</strong></div>
      <div className="ai-header-controls">
        {authorized && <select aria-label="Active model" value={model} onChange={(e) => setModel(e.target.value)} disabled={streaming || !models.length}>
          {models.map((choice) => <option key={choice.slug} value={choice.slug}>{choice.display_name}</option>)}
        </select>}
        <button className="icon" aria-label="New conversation" title="New conversation" disabled={streaming} onClick={() => void newConversation()}>＋</button>
        <details className="ai-menu"><summary aria-label="AI settings" title="AI settings">•••</summary>
          <div className="ai-menu-panel">
            <strong>ChatGPT · {connected && authorized ? account || 'Connected' : 'Not connected'}</strong>
            <label className="ai-consent"><input type="checkbox" checked={routingConsent} disabled={!authorized || consentUpdating || streaming} onChange={(e) => void updateConsent(e.target.checked)} />
              Allow prompt and bounded tool results to be sent to ChatGPT</label>
            <label className="ai-consent"><input type="checkbox" checked={toolsEnabled} disabled={streaming} onChange={(e) => setToolsEnabled(e.target.checked)} />
              Include Flowbench read-only tools in requests</label>
            <small className="ai-menu-note">Turn this off for a simple no-tools comparison. Normal assistant use keeps tools enabled.</small>
            {authorized && <>
              <button onClick={() => void refreshModels()} disabled={busy || streaming}>Refresh models</button>
              <button onClick={() => void signIn()} disabled={busy || streaming}>Reauthorize</button>
            </>}
            <button onClick={() => void loadResponseDiagnostics()} disabled={busy || streaming}>Show safe response diagnostics</button>
            {responseDiagnostics.length > 0 && <details className="ai-response-diagnostics" open>
              <summary>Last {responseDiagnostics.length} response summaries</summary>
              <p>Metadata only: prompts, response text, reasoning, tool arguments/results and credentials are excluded.</p>
              <pre>{JSON.stringify(responseDiagnostics, null, 2)}</pre>
            </details>}
            <hr />
            <strong>Inspect Flowbench</strong>
            {quickActions.map((entry) => <button key={entry.tool} disabled={busy || streaming} onClick={() => void localCommand(entry)}>{entry.label}</button>)}
            <form className="ai-search" onSubmit={(e) => { e.preventDefault(); void inspect({ label: 'Search catalogue', tool: 'catalog.search_tools', args: { query } }); }}>
              <label htmlFor="ai-catalog-search">Search tool catalogue</label>
              <div><input id="ai-catalog-search" type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter, summarize…" />
                <button disabled={!query.trim() || busy}>Search</button></div>
            </form>
            {connected ? <button className="danger" onClick={() => void signOut()} disabled={busy || streaming}>Disconnect ChatGPT</button> : <button onClick={() => void signIn()} disabled={busy}>Continue with ChatGPT</button>}
          </div>
        </details>
        {embedded && <button className="icon" onClick={close} aria-label="Close Flowbench AI">×</button>}
      </div>
    </header>

    <section className="ai-conversation" aria-label="ChatGPT conversation">
      <div className="ai-chat-messages" ref={chatScroll} aria-live="polite" onScroll={(e) => {
        const el = e.currentTarget;
        stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 56;
      }}>
        {messages.length === 0 && <div className="ai-empty">
          <div className="ai-orb" aria-hidden="true">✦</div>
          <h2>What would you like to analyse?</h2>
          <p>Flowbench AI can inspect this workflow and the installed tool catalogue.</p>
          {!authorized && <div className="ai-connect-inline">
            <span>{sessionRecovery ? 'Reconnect your protected ChatGPT session to continue.' : connected ? 'ChatGPT plan usage is not authorised yet.' : 'Connect an eligible ChatGPT account to start a conversation.'}</span>
            <button className="primary" onClick={() => void signIn()} disabled={busy}>{busy ? 'Opening sign-in…' : 'Continue with ChatGPT'}</button>
          </div>}
          {authorized && <div className="ai-examples">{[
            'What tools are implemented in Flowbench?',
            'What does the open workflow do?',
            'Why is this workflow invalid?',
          ].map((example) => <button key={example} disabled={!routingConsent || streaming} onClick={() => void send(example)}>{example}<span>→</span></button>)}</div>}
        </div>}
        {messages.map((message) => <Message key={message.id} message={message} onRetry={() => void retry(message)} />)}
        {result !== null && <details className="ai-local-result"><summary>Local inspection result</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>}
        {error && <div className="ai-inline-error" role="alert">{error}<button onClick={() => setError('')}>Dismiss</button></div>}
        {status === 'tool-running' && <div className="ai-live-activity" role="status">Working with Flowbench…</div>}
      </div>
      <footer className="ai-composer-wrap">
        <div className="ai-contextline"><span className={`routing-dot ${connected && authorized ? 'connected' : ''}`} />
          <span title={name}>{name}</span><span>·</span><span>{nodes} nodes</span>
          <span className="ai-tools-mode">{toolsEnabled ? 'Tools on' : 'No-tools probe'}</span>
          <span className={`ai-routing-label ${routingConsent ? '' : 'off'}`}>{routingConsent ? 'External AI routing allowed' : 'External AI routing off'}</span>
        </div>
        {authorized && <form className="ai-compose" onSubmit={(e) => { e.preventDefault(); void send(); }}>
          <textarea aria-label="Message Flowbench AI" rows={2} value={draft} onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(); } }}
            placeholder={routingConsent ? 'Ask about this workflow…' : 'Allow data routing in settings to start'} disabled={streaming || !routingConsent} />
          {streaming ? <button type="button" className="ai-stop" onClick={() => void cancel()} aria-label="Stop response">■</button> :
            <button className="primary" aria-label="Send" disabled={consentUpdating || !routingConsent || !draft.trim() || !model}>↑</button>}
        </form>}
        <div className="ai-composer-hint">Enter to send · Shift+Enter for a new line</div>
      </footer>
    </section>
    {busy && <span className="sr-only" role="status">Working…</span>}
  </aside>;
}

function Message({ message, onRetry }: { message: ChatMessage; onRetry: () => void }) {
  return <article className={`ai-message ${message.role}`} data-state={message.state} data-message-id={message.id}>
    <div className="ai-message-label">{message.role === 'user' ? 'You' : <><span className="ai-assistant-mark">✦</span> Flowbench AI</>}</div>
    {message.text ? <MarkdownText text={message.text} /> : message.state === 'streaming' ? <div className="ai-thinking"><i />Thinking…</div> : null}
    {!!message.activity?.length && <details className="ai-tool-activity"><summary>{message.activity.length === 1 ? message.activity[0] : `${message.activity.length} Flowbench actions`}</summary>
      {message.activity.length > 1 && <ul>{message.activity.map((activity, i) => <li key={`${i}_${activity}`}>{activity}</li>)}</ul>}</details>}
    {message.error && <div className="ai-message-error" role="alert">{message.error}<button onClick={onRetry}>Retry</button></div>}
    {message.state === 'cancelled' && <div className="ai-message-error">Response stopped</div>}
  </article>;
}

/** Small safe Markdown subset: React text nodes are used throughout; no HTML injection. */
function MarkdownText({ text }: { text: string }) {
  const blocks = text.split(/```([^\n]*)\n([\s\S]*?)```/g);
  return <div className="ai-markdown">{blocks.map((block, index) => {
    if (index % 3 === 2) return <pre className="ai-code" key={index}><code>{block}</code></pre>;
    if (index % 3 === 1) return null;
    return block.split(/\n{2,}/).filter(Boolean).map((paragraph, p) => <p key={`${index}_${p}`}>{paragraph.split('\n').map((line, i) => <span key={i}>{inlineMarkdown(line)}{i < paragraph.split('\n').length - 1 && <br />}</span>)}</p>);
  })}</div>;
}
function inlineMarkdown(line: string) {
  const parts = line.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
  return parts.map((part, i) => part.startsWith('`') && part.endsWith('`') ? <code key={i}>{part.slice(1, -1)}</code> :
    part.startsWith('**') && part.endsWith('**') ? <strong key={i}>{part.slice(2, -2)}</strong> : part);
}
function activityLabel(tool: string) {
  if (tool === 'workflow.get_snapshot' || tool === 'app.get_active_workflow') return 'Inspecting workflow…';
  if (tool === 'catalog.search_tools' || tool === 'catalog.get_tool_manifest' || tool === 'app.get_capabilities') return 'Searching tool catalogue…';
  if (tool === 'workflow.validate' || tool === 'workflow.get_diagnostics') return 'Checking validation…';
  return 'Inspecting Flowbench…';
}
function messageOf(error: unknown) { return error instanceof Error ? error.message : String(error); }
function emptyResponseMessage(reason?: string) {
  switch (reason) {
    case 'reasoning_only': return 'ChatGPT completed with reasoning output but no user-facing text. Open AI settings → Show safe response diagnostics and share the metadata summary.';
    case 'missing_terminal_output': return 'ChatGPT reported completion without a usable output list. Open AI settings → Show safe response diagnostics.';
    case 'message_without_user_facing_text': return 'ChatGPT completed a message without user-facing text. Open AI settings → Show safe response diagnostics.';
    case 'streamed_text_unavailable': return 'ChatGPT emitted text events, but Flowbench could not recover their text. Open AI settings → Show safe response diagnostics.';
    default: return 'ChatGPT completed without user-facing text. Open AI settings → Show safe response diagnostics before retrying.';
  }
}
