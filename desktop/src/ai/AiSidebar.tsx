import { useEffect, useRef, useState } from 'react';
import { dispatchAgentTool, type AgentToolName } from './protocol';
import { useEditor } from '../state/store';
import { cancelRun, validateNow } from '../state/engineActions';
import { ensureAiStreamListener, useAiConversation, type ChatMessage } from './conversation';
import { prepareWorkflowProposal, workflowRevision } from './workflowPatch';
import { runWorkflow } from '../state/engineActions';
import { doSave } from '../panels/Toolbar';

type Entry = { label: string; tool: AgentToolName; args?: Record<string, unknown> };
type Model = { slug: string; display_name: string; supported_efforts?: string[]; default_effort?: string | null };
const EFFORT_KEY = 'flowbench.ai.effort';
const FALLBACK_EFFORTS = ['low', 'medium', 'high'];
const effortLabel = (effort: string) => effort === 'xhigh' ? 'Extra high' : effort[0].toUpperCase() + effort.slice(1);
function storedEffort() { try { return localStorage.getItem(EFFORT_KEY) || 'low'; } catch { return 'low'; } }
/** The cheapest sensible level the model supports: the user's choice, else low, else the lowest offered. */
function effortFor(model: Model | undefined, preferred: string) {
  const choices = model?.supported_efforts?.length ? model.supported_efforts : FALLBACK_EFFORTS;
  if (choices.includes(preferred)) return preferred;
  return choices.includes('low') ? 'low' : choices[0];
}
type ValueItem = { type?: string; name?: string; arguments?: string; call_id?: string; [key: string]: unknown };
type NativeResponse = { turn_id: string; message_id: string; cycle_id: string; status?: 'retryable_stream_interruption'; needs_tool_correction?: boolean; text?: string; text_source?: string; empty_reason?: string; calls?: ValueItem[] };
class ToolCorrectionFailure extends Error {
  constructor() {
    super('ChatGPT could not correct its tool arguments after three attempts. Rejected actions were not executed; earlier completed actions were kept. Retry to continue.');
  }
}
class StreamInterruption extends Error {
  constructor() {
    super('The ChatGPT connection was interrupted after two automatic retries. Completed workflow actions were kept. Retry to continue from the current state.');
  }
}
const quickActions: Entry[] = [
  { label: 'Inspect this workflow', tool: 'workflow.get_snapshot' },
  { label: 'Check validation', tool: 'workflow.validate' },
  { label: 'Read diagnostics', tool: 'workflow.get_diagnostics' },
  { label: 'List implemented tools', tool: 'app.get_capabilities' },
];
let resolveRunApproval: ((value: unknown) => void) | null = null;
let resolvePatchApproval: ((value: unknown) => void) | null = null;
let resolveSaveApproval: ((value: unknown) => void) | null = null;

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
  const [preferredEffort, setPreferredEffort] = useState(storedEffort);
  const [draft, setDraft] = useState('');
  const [routingConsent, setRoutingConsent] = useState(false);
  const [toolsEnabled, setToolsEnabled] = useState(true);
  const [consentUpdating, setConsentUpdating] = useState(false);
  const [error, setError] = useState('');
  const [responseDiagnostics, setResponseDiagnostics] = useState<unknown[]>([]);
  const activeModel = models.find((choice) => choice.slug === model);
  const effortChoices = activeModel?.supported_efforts?.length ? activeModel.supported_efforts : FALLBACK_EFFORTS;
  const effort = effortFor(activeModel, preferredEffort);
  function chooseEffort(value: string) {
    setPreferredEffort(value);
    try { localStorage.setItem(EFFORT_KEY, value); } catch { /* the choice still applies to this session */ }
  }
  const messages = useAiConversation((s) => s.messages);
  const proposal = useAiConversation((s) => s.proposal);
  const runApprovalRequested = useAiConversation((s) => s.runApprovalRequested);
  const saveApprovalRequested = useAiConversation((s) => s.saveApprovalRequested);
  const fullControlScope = useAiConversation((s) => s.fullControlScope);
  const status = useAiConversation((s) => s.status);
  const chatScroll = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const cancelled = useRef(false);
  const name = useEditor((s) => s.name);
  const baseDir = useEditor((s) => s.baseDir);
  const filePath = useEditor((s) => s.filePath);
  const nodes = useEditor((s) => s.nodes.length);
  const controlScope = baseDir || filePath || `untitled:${name}`;
  const fullControl = fullControlScope === controlScope;
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
      useAiConversation.getState().setFullControl(null);
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
      const value = await dispatchAgentTool(agentEnvironment(), entry.tool, entry.args);
      setResult(value);
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  }
  function agentEnvironment() {
    const originatingTurnId = useAiConversation.getState().turnId;
    return {
      getState: () => useEditor.getState(),
      validate: async () => { await validateNow(); },
      proposePatch: async (patchJson: string, baseRevision: string) => {
        const prepared = await prepareWorkflowProposal(useEditor.getState(), patchJson, baseRevision);
        // Validation can outlive Stop or a replaced conversation turn.
        // Never apply an asynchronously prepared patch after either occurs.
        if (cancelled.current || (originatingTurnId && useAiConversation.getState().turnId !== originatingTurnId))
          return { status: 'cancelled', operation_count: prepared.changes.length, valid: false, problem_codes: [] };
        if (fullControlForCurrentWorkspace()) return applyProposal(prepared, false);
        if (resolvePatchApproval) throw new Error('A graph proposal is already awaiting approval');
        useAiConversation.getState().setProposal(prepared);
        return new Promise((resolve) => { resolvePatchApproval = resolve; });
      },
      requestRun: () => {
        if (fullControlForCurrentWorkspace()) return executeRunForAgent();
        if (useEditor.getState().run.status === 'running') return Promise.resolve({ status: 'already_running', outputs: [] });
        if (resolveRunApproval) throw new Error('A workflow run is already awaiting user approval');
        useAiConversation.getState().requestRunApproval();
        return new Promise((resolve) => { resolveRunApproval = resolve; });
      },
      requestSave: () => {
        if (fullControlForCurrentWorkspace()) return saveForAgent();
        if (resolveSaveApproval) throw new Error('A workflow save is already awaiting approval');
        useAiConversation.getState().requestSaveApproval();
        return new Promise((resolve) => { resolveSaveApproval = resolve; });
      },
      getResults: () => safeRunResults(),
      getResultPage: (nodeRef: string, offset: number, limit: number) => safeRunResultPage(nodeRef, offset, limit),
    };
  }
  function fullControlForCurrentWorkspace() {
    const state = useEditor.getState();
    const scope = state.baseDir || state.filePath || `untitled:${state.name}`;
    return useAiConversation.getState().fullControlScope === scope;
  }
  function setFullControl(enabled: boolean) {
    if (enabled) {
      const accepted = window.confirm('Enable Full Control for this Flowbench workspace for the current app session? Flowbench AI may discover installed Flowbench tools, add/configure/connect/remove workflow nodes, validate and run workflows, inspect results, and save to the current workflow path without asking each time. Changes remain undoable.');
      if (!accepted) return;
      useAiConversation.getState().setFullControl(controlScope);
    } else useAiConversation.getState().setFullControl(null);
  }
  async function applyProposal(pending: import('./workflowPatch').WorkflowProposal, requireReviewConfirmation: boolean) {
    if (workflowRevision(useEditor.getState()) !== pending.baseRevision)
      return { status: 'stale', operation_count: pending.changes.length, valid: false, problem_codes: [] };
    if (!pending.validation.valid)
      return { status: 'validation_failed', operation_count: pending.changes.length, valid: false,
        problem_codes: pending.validation.problems.map((problem) => problem.code).slice(0, 20) };
    if (requireReviewConfirmation && pending.consequences.length &&
        !window.confirm(`Review these consequential changes before applying:\n\n${pending.consequences.join('\n')}\n\nApply the validated graph change?`)) return null;
    useEditor.getState().commitGraphTransaction(pending.candidate);
    const committedRevision = workflowRevision(useEditor.getState());
    useAiConversation.getState().setProposal(null);
    const validation = await validateNow();
    if (!validation?.valid) {
      if (workflowRevision(useEditor.getState()) === committedRevision) useEditor.getState().undo();
      else setError('Post-commit validation failed after the workflow was edited again. The newer editor state was preserved.');
    }
    return { status: validation?.valid ? 'approved' : 'revalidation_failed', operation_count: pending.changes.length,
      valid: Boolean(validation?.valid), problem_codes: validation?.problems.map((problem) => problem.code).slice(0, 20) ?? [] };
  }
  async function approvePatch() {
    const pending = useAiConversation.getState().proposal;
    if (!pending) return;
    const outcome = await applyProposal(pending, true);
    if (!outcome) return;
    if (outcome.status === 'stale') {
      setError('The workflow changed after this preview. Ask Flowbench AI to prepare a fresh proposal.');
      useAiConversation.getState().setProposal(null);
    } else if (outcome.status === 'validation_failed') {
      setError('This proposal has validation errors and cannot be applied.');
      return;
    }
    resolvePatchApproval?.(outcome); resolvePatchApproval = null;
  }
  function rejectPatch() {
    const pending = useAiConversation.getState().proposal;
    useAiConversation.getState().setProposal(null);
    resolvePatchApproval?.({ status: 'rejected', operation_count: pending?.changes.length ?? 0, valid: false, problem_codes: [] }); resolvePatchApproval = null;
  }
  async function approveRun() {
    useAiConversation.setState({ runApprovalRequested: false });
    const initial = useEditor.getState().run;
    await runWorkflow();
    const started = Date.now();
    while (true) {
      const run = useEditor.getState().run;
      if (run.runId && run.runId !== initial.runId && run.status !== 'running') {
        resolveRunApproval?.(safeRunResults()); resolveRunApproval = null; return;
      }
      if (!run.runId && run.status !== 'running' && run.status !== initial.status) {
        resolveRunApproval?.(safeRunResults()); resolveRunApproval = null; return;
      }
      if (run.status !== 'running' && Date.now() - started > 1500 && run.runId === initial.runId && run.status === initial.status) {
        resolveRunApproval?.({ status: 'not_started', outputs: [] }); resolveRunApproval = null; return;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    resolveRunApproval?.({ status: 'timed_out', outputs: [] }); resolveRunApproval = null;
  }
  async function executeRunForAgent() {
    const initial = useEditor.getState().run;
    if (initial.status === 'running') return { status: 'already_running', outputs: [] };
    await runWorkflow();
    const started = Date.now();
    while (Date.now() - started < 15 * 60_000) {
      const run = useEditor.getState().run;
      if (run.runId && run.runId !== initial.runId && run.status !== 'running') return safeRunResults();
      if (run.status === 'running') {
        if (run.runId && run.runId !== initial.runId) {
          await new Promise((resolve) => setTimeout(resolve, 250));
          continue;
        }
      } else if (run.status !== initial.status) return safeRunResults();
      if (Date.now() - started > 1500 && run.runId === initial.runId && run.status === initial.status)
        return { status: 'not_started', outputs: [] };
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
  async function saveForAgent() {
    const outcome = await doSave();
    return { status: outcome === 'failed' ? 'save_failed' : outcome, path_selected: outcome === 'saved' };
  }
  function rejectRun() {
    useAiConversation.setState({ runApprovalRequested: false });
    resolveRunApproval?.({ status: 'rejected', outputs: [] }); resolveRunApproval = null;
  }
  async function approveSave() {
    useAiConversation.setState({ saveApprovalRequested: false });
    const outcome = await doSave();
    resolveSaveApproval?.({ status: outcome, path_selected: outcome === 'saved' });
    resolveSaveApproval = null;
  }
  function rejectSave() {
    useAiConversation.setState({ saveApprovalRequested: false });
    resolveSaveApproval?.({ status: 'rejected', path_selected: false }); resolveSaveApproval = null;
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
    let correctionAttempts = 0;
    try {
      await ensureAiStreamListener();
      const started = await native<{ turn_id: string; assistant_message_id: string }>('ai_begin', { prompt: text });
      turnId = started.turn_id;
      useAiConversation.getState().begin(
        { id: `user_${turnId}`, turnId, role: 'user', text, state: 'completed' },
        { id: started.assistant_message_id, turnId, role: 'assistant', text: '', state: 'streaming' },
      );
      for (let cycle = 0; !cancelled.current; cycle++) {
        let response: NativeResponse | undefined;
        let cycleId = '';
        for (let attempt = 0; attempt < 3 && !cancelled.current; attempt++) {
          cycleId = `cycle_${crypto.randomUUID().replaceAll('-', '')}`;
          useAiConversation.getState().responseStart(turnId, cycleId);
          response = await native<NativeResponse>('ai_respond', { model, effort, turnId, cycleId, toolsEnabled });
          if (response.turn_id !== turnId || response.message_id !== started.assistant_message_id || response.cycle_id !== cycleId) throw new Error('The response belonged to a different conversation turn.');
          if (cancelled.current) break;
          if (response.status !== 'retryable_stream_interruption') break;
          useAiConversation.getState().discardResponse(turnId, cycleId);
          if (attempt === 2) throw new StreamInterruption();
          useAiConversation.getState().activity(turnId, `Connection interrupted; reconnecting (${attempt + 1}/2)…`);
          await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)));
        }
        if (cancelled.current) break;
        if (!response) throw new Error('ChatGPT returned no response.');
        if (response.needs_tool_correction) {
          useAiConversation.getState().discardResponse(turnId, cycleId);
          correctionAttempts++;
          if (correctionAttempts >= 3) throw new ToolCorrectionFailure();
          useAiConversation.getState().activity(turnId, `Correcting rejected tool arguments (${correctionAttempts}/3)…`);
          continue;
        }
        correctionAttempts = 0;
        // Completed output is authoritative. This recovers text if event delivery was late or lost.
        useAiConversation.getState().authoritative(turnId, response.message_id, cycleId, response.text ?? '');
        const calls = response.calls ?? [];
        if (!calls.length) {
          if (!response.text?.trim()) throw new Error(emptyResponseMessage(response.empty_reason));
          await native('ai_finish_turn', { turnId });
          useAiConversation.getState().finish(turnId, 'completed');
          return;
        }
          for (const call of calls) {
          const toolName = call.name ?? '';
          useAiConversation.getState().activity(turnId, activityLabel(toolName));
          let toolResult: unknown;
          try {
            toolResult = await dispatchAgentTool(agentEnvironment(), toolName,
              JSON.parse(call.arguments ?? '{}'));
          } catch (error) {
            toolResult = safeToolFailure(toolName, error);
          }
          await native('ai_submit_tool_result', { callId: call.call_id, result: toolResult });
        }
      }
      if (cancelled.current && turnId) {
        await native('ai_abort_turn', { turnId }).catch(() => undefined);
        useAiConversation.getState().finish(turnId, 'cancelled');
      }
    } catch (e) {
      const message = messageOf(e);
      if (turnId) {
        const canContinue = (e instanceof StreamInterruption || e instanceof ToolCorrectionFailure) && !cancelled.current;
        await native(canContinue ? 'ai_finish_turn' : 'ai_abort_turn', { turnId }).catch(() => undefined);
        useAiConversation.getState().finish(turnId, cancelled.current ? 'cancelled' : 'failed', cancelled.current ? undefined : message, canContinue ? 'continue' : undefined);
      } else if (!cancelled.current) {
        setDraft(text);
        setError(message);
      }
    }
  }
  async function cancel() {
    cancelled.current = true;
    useAiConversation.getState().setProposal(null);
    resolvePatchApproval?.({ status: 'cancelled', operation_count: 0, valid: false, problem_codes: [] }); resolvePatchApproval = null;
    resolveSaveApproval?.({ status: 'cancelled', path_selected: false }); resolveSaveApproval = null;
    resolveRunApproval?.({ status: 'cancelled', outputs: [] }); resolveRunApproval = null;
    if (useEditor.getState().run.status === 'running') await cancelRun().catch(() => undefined);
    try { await native('ai_cancel'); } catch { /* request may already have ended */ }
  }
  async function retry(message: ChatMessage) {
    if (message.retryMode === 'continue') {
      await send('Continue my interrupted request from the current workflow and the completed tool results already in this conversation. Inspect the current state first and carry out only the remaining work; do not repeat successful changes or claim unverified results.');
      return;
    }
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
        {authorized && <select aria-label="Reasoning effort" title="Reasoning effort: higher levels think longer and use more of the plan's limits" value={effort}
          onChange={(e) => chooseEffort(e.target.value)} disabled={streaming || !models.length}>
          {effortChoices.map((choice) => <option key={choice} value={choice}>{effortLabel(choice)}</option>)}
        </select>}
        <button className="icon" aria-label="New conversation" title="New conversation" disabled={streaming} onClick={() => void newConversation()}>＋</button>
        <details className="ai-menu"><summary aria-label="AI settings" title="AI settings">•••</summary>
          <div className="ai-menu-panel">
            <strong>ChatGPT · {connected && authorized ? account || 'Connected' : 'Not connected'}</strong>
            <section className="ai-full-control" aria-label="Full Control mode">
              <label className="ai-consent"><input type="checkbox" checked={fullControl} disabled={busy || !authorized || (streaming && !fullControl)}
                onChange={(e) => setFullControl(e.target.checked)} />Full Control for this workspace</label>
              <small className="ai-menu-note">{fullControl
                ? 'Enabled for this workspace during this app session. Turn it off here at any time.'
                : 'Review mode asks before workflow edits, runs and saves. Full Control removes routine prompts; external effects remain separately protected.'}</small>
            </section>
            <label className="ai-consent"><input type="checkbox" checked={routingConsent} disabled={!authorized || consentUpdating || streaming} onChange={(e) => void updateConsent(e.target.checked)} />
              Allow prompts, workflow context and requested data results to be sent to ChatGPT</label>
            <label className="ai-consent"><input type="checkbox" checked={toolsEnabled} disabled={streaming} onChange={(e) => setToolsEnabled(e.target.checked)} />
              Include Flowbench application tools in requests</label>
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
          <p>{fullControl ? 'Flowbench AI can build, validate and run workflows in this workspace. External effects remain separately protected.' : 'Flowbench AI can build and validate workflows using installed tools. Enable Full Control in settings to delegate routine workspace operations.'}</p>
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
        {proposal && <section className="ai-proposal-card" aria-label="Workflow change preview">
          <strong>Workflow change preview</strong>
          <ul>{proposal.changes.map((change, i) => <li key={`${i}_${change}`}>{change}</li>)}</ul>
          {!!proposal.consequences.length && <p><strong>Consequences:</strong> {proposal.consequences.join('; ')}</p>}
          <p role={proposal.validation.valid ? 'status' : 'alert'}>{proposal.validation.valid
            ? 'The engine validated this candidate graph. The saved workflow has not changed.'
            : `Engine validation found ${proposal.validation.problems.length} issue(s): ${proposal.validation.problems.map((p) => p.message).join('; ')}`}</p>
          <div><button disabled={!proposal.validation.valid} className="primary" onClick={approvePatch}>Approve changes</button>
            <button onClick={rejectPatch}>Reject</button></div>
        </section>}
        {runApprovalRequested && <section className="ai-proposal-card" aria-label="Run approval">
          <strong>Run this workflow?</strong><p>Flowbench will execute the current saved editor graph. Existing code and database-write confirmations still apply.</p>
          <div><button className="primary" onClick={() => void approveRun()} disabled={useEditor.getState().run.status === 'running'}>Approve and run</button>
            <button onClick={rejectRun}>Cancel</button></div>
        </section>}
        {saveApprovalRequested && <section className="ai-proposal-card" aria-label="Save approval">
          <strong>Save this workflow?</strong><p>Flowbench will open its normal save destination prompt. The workflow is written only after you confirm the path.</p>
          <div><button className="primary" onClick={() => void approveSave()}>Choose destination and save</button>
            <button onClick={rejectSave}>Cancel</button></div>
        </section>}
        {result !== null && <details className="ai-local-result"><summary>Local inspection result</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>}
        {error && <div className="ai-inline-error" role="alert">{error}<button onClick={() => setError('')}>Dismiss</button></div>}
        {status === 'tool-running' && <div className="ai-live-activity" role="status">Working with Flowbench…</div>}
      </div>
      <footer className="ai-composer-wrap">
        <div className="ai-contextline"><span className={`routing-dot ${connected && authorized ? 'connected' : ''}`} />
          <span title={name}>{name}</span><span>·</span><span>{nodes} nodes</span>
          <span className="ai-tools-mode" title={fullControl ? 'Full Control is enabled for the active workspace during this app session' : undefined}>{fullControl ? 'Full Control' : toolsEnabled ? 'Tools on' : 'No-tools probe'}</span>
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

function safeRunResults() {
  const run = useEditor.getState().run;
  if (run.status !== 'succeeded' || !run.result) return { status: run.status, outputs: [] };
  const allOutputs = Object.entries(run.result.browse ?? {});
  const outputs = allOutputs.map(([nodeId, preview]) => ({
    node_ref: `node_${useEditor.getState().nodes.findIndex((node) => node.id === nodeId) + 1}`,
    fields: preview.fields.map((field) => ({ name: field.name, type: field.type })),
    fields_truncated: false,
    total_rows: preview.total_rows,
    available_rows: preview.rows.length,
    rows: preview.rows.slice(0, 1),
  }));
  return { status: 'succeeded', outputs, outputs_truncated: false };
}

function safeRunResultPage(nodeRef: string, offset: number, limit: number) {
  const run = useEditor.getState().run;
  if (run.status !== 'succeeded' || !run.result) throw new Error('No successful workflow result is available');
  const index = Number(nodeRef.match(/^node_(\d+)$/)?.[1] ?? 0) - 1;
  const node = useEditor.getState().nodes[index];
  if (!node) throw new Error('Unknown Browse result node reference');
  const preview = run.result.browse?.[node.id];
  if (!preview) throw new Error('The selected node has no Browse output in the latest run');
  const safeFields = preview.fields.map((field, i) => ({ field, i })).filter(({ field }) =>
    !/password|passwd|secret|token|credential|api.?key|access.?key/i.test(field.name));
  const keep = safeFields;
  return {
    node_ref: nodeRef, offset, total_rows: preview.total_rows, available_rows: preview.rows.length,
    fields: keep.map(({ field }) => ({ name: field.name, type: field.type })), fields_truncated: safeFields.length > keep.length,
    rows: preview.rows.slice(offset, offset + limit).map((row) => keep.map(({ i }) => {
      return row[i];
    })),
  };
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
  if (tool === 'workflow.propose_patch') return 'Preparing a validated workflow change…';
  if (tool === 'workflow.request_run') return 'Waiting for run approval and execution…';
  if (tool === 'run.get_results') return 'Reading actual workflow results…';
  if (tool === 'run.get_result_page') return 'Reading the requested result page…';
  if (tool === 'workflow.get_schema') return 'Inspecting output fields and types…';
  if (tool === 'workflow.get_snapshot' || tool === 'app.get_active_workflow') return 'Inspecting workflow…';
  if (tool === 'catalog.search_tools' || tool === 'catalog.get_tool_manifest' || tool === 'app.get_capabilities') return 'Searching tool catalogue…';
  if (tool === 'workflow.validate' || tool === 'workflow.get_diagnostics') return 'Checking validation…';
  return 'Inspecting Flowbench…';
}
function safeToolFailure(tool: string, error: unknown) {
  const text = error instanceof Error ? error.message : '';
  const contract = text.match(/Flowbench result contract violation for [a-z0-9_.-]+ at (\$[a-z0-9_.$-]+)/i);
  return {
    tool,
    code: contract ? 'result_contract_violation' : 'tool_execution_failed',
    stage: contract ? 'result_validation' : 'flowbench_dispatch',
    error: contract ? `Result violated the registered shape at ${contract[1]}.` : 'The Flowbench capability failed; inspect its current state and retry if appropriate.',
  };
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
