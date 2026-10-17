import { useEffect, useRef, useState } from 'react';
import { dispatchAgentTool, type AgentToolName } from './protocol';
import { useEditor } from '../state/store';
import { cancelRun, validateNow } from '../state/engineActions';
import { ensureAiStreamListener, useAiConversation, type ChatMessage, type Verification } from './conversation';
import { prepareWorkflowProposal, workflowRevision } from './workflowPatch';
import { engine, runWorkflow } from '../state/engineActions';
import { doSave } from '../panels/Toolbar';
import { activityLabel, addUsage, diagnosticSummary, NO_USAGE, tokens, type Usage } from './panelText';
import { ProviderSettings, providerLabel, type LocalProvider } from './ProviderSettings';
import { STATUS_TEXT, TaskView } from './TaskView';
import type { AgentTask } from './conversation';

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
type NativeResponse = { turn_id: string; message_id: string; cycle_id: string; status?: 'retryable_stream_interruption'; needs_tool_correction?: boolean;
  needs_execution?: boolean; continue?: boolean; text?: string; text_source?: string; empty_reason?: string; calls?: ValueItem[];
  usage?: { input_tokens?: number | null; output_tokens?: number | null } };
type AuthStatus = { provider?: 'chatgpt' | 'local'; local?: LocalProvider; chatgpt_connected?: boolean; connected: boolean; plan_usage_authorized: boolean;
  subject_hint?: string; session_recovery_required?: boolean; data_routing_allowed?: boolean };
/** A turn above this many tokens is flagged: it is using a large share of a plan's limits. */
const HEAVY_TURN_TOKENS = 200_000;
class ToolCorrectionFailure extends Error {
  constructor() {
    super('The model could not correct its tool arguments after three attempts. Rejected actions were not executed; earlier completed actions were kept. Retry to continue.');
  }
}
class StreamInterruption extends Error {
  constructor() {
    super('The connection to the model was interrupted after two automatic retries. Completed workflow actions were kept. Retry to continue from the current state.');
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
let resolveActionApproval: ((approved: boolean) => void) | null = null;
/** Permissions the user approved during the current task; cleared when a new request starts. */
const taskGrants = new Set<string>();
function describeOperation(operation: string, args: Record<string, unknown>) {
  if (operation === 'download_file') return `Download ${String(args.url ?? '')} into ${String(args.folder ?? 'data')}/`;
  if (operation === 'run_command') return `Run: ${(args.argv as string[] | undefined)?.join(' ') ?? ''}`;
  if (operation === 'run_python') return `Run this Python script in the workspace:\n${String(args.code ?? '').split('\n').slice(0, 12).join('\n')}`;
  if (operation === 'write_text_file') return `Write ${String(args.path ?? '')}${args.overwrite ? ' (replacing the existing file)' : ''}`;
  if (operation === 'extract_archive') return `Extract ${String(args.path ?? '')}`;
  if (operation === 'make_folder') return `Create the folder ${String(args.path ?? '')}`;
  return `${operation.replaceAll('_', ' ')} ${JSON.stringify(args).slice(0, 300)}`;
}

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
  const [showDiagnosticJson, setShowDiagnosticJson] = useState(false);
  const [local, setLocal] = useState<LocalProvider | null>(null);
  const [chatgptConnected, setChatgptConnected] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [turnUsage, setTurnUsage] = useState<Usage>(NO_USAGE);
  const [sessionUsage, setSessionUsage] = useState<Usage>(NO_USAGE);
  const [lastInputTokens, setLastInputTokens] = useState(0);
  const [view, setView] = useState<'chat' | 'task'>('chat');
  const [recentTasks, setRecentTasks] = useState<AgentTask[]>([]);
  const [paused, setPaused] = useState(false);
  const pausedRef = useRef(false);
  const task = useAiConversation((s) => s.task);
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
  const actionApproval = useAiConversation((s) => s.actionApproval);
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
      const auth = await native<AuthStatus>('ai_status');
      setConnected(auth.connected); setAuthorized(auth.plan_usage_authorized); setAccount(auth.subject_hint ?? '');
      setLocal(auth.provider === 'local' && auth.local ? auth.local : null);
      setChatgptConnected(auth.provider === 'local' ? Boolean(auth.chatgpt_connected) : auth.connected);
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
      setConnected(false); setAuthorized(false); setAccount(''); setModels([]); setModel(''); setRoutingConsent(false); setChatgptConnected(false);
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
      getResultPage: (nodeRef: string, offset: number, limit: number, columns: string) => safeRunResultPage(nodeRef, offset, limit, columns),
      callAgentOperation: (operation: string, args: Record<string, unknown>) => callAgentOperation(operation, args),
    };
  }
  /** Runs an engine operation. Network and file-writing operations are granted under Full Control
   *  and otherwise approved by the user for that call; the engine refuses them without the grant. */
  async function callAgentOperation(operation: string, args: Record<string, unknown>) {
    const call = (permissions: string[]) => engine().request<unknown>('agent.call',
      { name: operation, arguments: args, base_dir: useEditor.getState().baseDir, permissions });
    const granted = new Set([...(fullControlForCurrentWorkspace() ? ['network', 'file_writes'] : []), ...taskGrants]);
    const first = await call([...granted]) as { ok: boolean; error?: { code: string; permissions?: string[] } };
    if (first.ok || first.error?.code !== 'permission-required') return first;
    const needed = first.error.permissions ?? [];
    const approved = await new Promise<boolean>((resolve) => {
      if (resolveActionApproval) { resolve(false); return; }
      resolveActionApproval = resolve;
      useAiConversation.getState().requestActionApproval({ operation, permissions: needed, detail: describeOperation(operation, args) });
    });
    if (!approved) return { ok: false, operation, error: { code: 'permission-declined', message: `The user declined ${needed.join(' and ')} for ${operation}. Ask before trying again, or continue another way.` } };
    // An approval holds for the rest of this task, so a multi-step job is not interrupted at every step.
    needed.forEach((p) => taskGrants.add(p));
    return call([...new Set([...granted, ...needed])]);
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
      if (!entries.length) setError('No responses have been recorded in this session yet.');
    } catch (e) { setError(messageOf(e)); }
  }
  async function send(textOverride?: string) {
    const text = (textOverride ?? draft).trim();
    if (!text || !model || streaming || !authorized || !routingConsent) return;
    stickToBottom.current = true;
    setError(''); setDraft(''); cancelled.current = false; setTurnUsage(NO_USAGE); taskGrants.clear();
    let turnId: string | undefined;
    let correctionAttempts = 0;
    let executionChecks = 0;
    let internalSteps = 0;
    try {
      await ensureAiStreamListener();
      const started = await native<{ turn_id: string; assistant_message_id: string; task?: AgentTask }>('ai_begin',
        { prompt: text, workspace: useEditor.getState().baseDir || null });
      turnId = started.turn_id;
      if (started.task) useAiConversation.getState().setTask(started.task);
      pausedRef.current = false; setPaused(false);
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
          response = await native<NativeResponse>('ai_respond', { model, effort: local ? null : effort, turnId, cycleId, toolsEnabled });
          if (response.usage) {
            const used = response.usage;
            setTurnUsage((total) => addUsage(total, used)); setSessionUsage((total) => addUsage(total, used));
            if (used.input_tokens) setLastInputTokens(used.input_tokens);
          }
          if (response.turn_id !== turnId || response.message_id !== started.assistant_message_id || response.cycle_id !== cycleId) throw new Error('The response belonged to a different conversation turn.');
          if (cancelled.current) break;
          if (response.status !== 'retryable_stream_interruption') break;
          useAiConversation.getState().discardResponse(turnId, cycleId);
          if (attempt === 2) throw new StreamInterruption();
          useAiConversation.getState().activity(turnId, `Connection interrupted; reconnecting (${attempt + 1}/2)…`);
          await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)));
        }
        if (cancelled.current) break;
        if (!response) throw new Error('The model returned no response.');
        if (response.needs_tool_correction) {
          useAiConversation.getState().discardResponse(turnId, cycleId);
          correctionAttempts++;
          if (correctionAttempts >= 3) throw new ToolCorrectionFailure();
          useAiConversation.getState().activity(turnId, `Correcting rejected tool arguments (${correctionAttempts}/3)…`);
          continue;
        }
        if (response.continue) {
          if (++internalSteps > 5) throw new Error('The model kept reclassifying the task without acting. Retry to continue.');
          continue;
        }
        if (response.needs_execution) {
          // The task controller rejected a text-only answer to an execution request.
          useAiConversation.getState().discardResponse(turnId, cycleId);
          executionChecks++;
          useAiConversation.getState().activity(turnId, `No Flowbench run backs that answer yet; asking the AI to run the workflow (${executionChecks}/2)…`);
          continue;
        }
        correctionAttempts = 0;
        // Completed output is authoritative. This recovers text if event delivery was late or lost.
        useAiConversation.getState().authoritative(turnId, response.message_id, cycleId, response.text ?? '');
        const calls = response.calls ?? [];
        if (!calls.length) {
          if (!response.text?.trim()) throw new Error(emptyResponseMessage(response.empty_reason));
          const task = await native<TaskRecord | null>('ai_finish_turn', { turnId });
          useAiConversation.getState().finish(turnId, 'completed', undefined, undefined, verificationOf(task));
          return;
        }
          for (const call of calls) {
          // Pause holds the AI between actions; the user can edit the workflow meanwhile.
          while (pausedRef.current && !cancelled.current) await new Promise((resolve) => setTimeout(resolve, 200));
          if (cancelled.current) break;
          const toolName = call.name ?? '';
          let args: Record<string, unknown> = {};
          try { args = JSON.parse(call.arguments ?? '{}'); } catch { /* dispatch reports the bad arguments */ }
          useAiConversation.getState().activity(turnId, activityLabel(toolName, args, useEditor.getState()));
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
  function togglePause() {
    pausedRef.current = !pausedRef.current;
    setPaused(pausedRef.current);
  }
  async function takeOver() {
    await cancel();
    if (fullControl) useAiConversation.getState().setFullControl(null);
  }
  async function openTasks() {
    setView('task');
    try {
      const tasks = await native<AgentTask[] | null>('ai_recent_tasks');
      setRecentTasks(Array.isArray(tasks) ? tasks : []);
    } catch { setRecentTasks([]); }
  }
  async function cancel() {
    cancelled.current = true;
    pausedRef.current = false; setPaused(false);
    useAiConversation.getState().setProposal(null);
    resolvePatchApproval?.({ status: 'cancelled', operation_count: 0, valid: false, problem_codes: [] }); resolvePatchApproval = null;
    resolveSaveApproval?.({ status: 'cancelled', path_selected: false }); resolveSaveApproval = null;
    resolveRunApproval?.({ status: 'cancelled', outputs: [] }); resolveRunApproval = null;
    resolveActionApproval?.(false); resolveActionApproval = null; useAiConversation.getState().requestActionApproval(null);
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
  async function addFiles() {
    try {
      const dlg = await import('@tauri-apps/plugin-dialog');
      const picked = await dlg.open({ title: 'Add data files for Flowbench AI', multiple: true, defaultPath: baseDir || undefined });
      const paths = (Array.isArray(picked) ? picked : picked ? [picked] : []).map(String);
      if (paths.length) setDraft((text) => `${text.trimEnd()}${text.trim() ? '\n\n' : ''}Data files:\n${paths.map((path) => `- ${path}`).join('\n')}\n`);
    } catch (e) { setError(messageOf(e)); }
  }
  async function providerChanged() {
    useAiConversation.getState().reset(); setResult(null); setTurnUsage(NO_USAGE); setLastInputTokens(0);
    setModels([]); setModel('');
    await refreshAuth();
  }

  const providerName = providerLabel(local);
  const ready = connected && authorized;
  const turnTotal = turnUsage.input + turnUsage.output;
  const nearContext = Boolean(local && lastInputTokens > local.context_tokens * 0.85);
  const lastProblem = responseDiagnostics.length ? diagnosticSummary(responseDiagnostics[responseDiagnostics.length - 1]) : null;
  const examples = [
    'Explain what the open workflow does',
    'Why is this workflow invalid?',
    'Which tool should I use to fuzzy-match customer names?',
    'Build a workflow that totals sales by region from the file I add',
  ];
  return <aside className={`${embedded ? 'ai-content' : 'app-right'} ai-sidebar`} aria-label="Flowbench AI workspace" data-testid="ai-sidebar">
    <header className="ai-head">
      <div className="ai-head-row">
        <div className="ai-brand"><span className={`routing-dot ${ready ? 'connected' : ''}`} title={ready ? `${providerName} connected` : `${providerName} not connected`} />
          <strong>Flowbench AI</strong><span className="ai-provider-badge">{providerName}</span></div>
        <div className="ai-header-controls">
          <button className="icon" aria-label="New conversation" title="New conversation" disabled={streaming} onClick={() => void newConversation()}>＋</button>
          <button className={`icon ${settingsOpen ? 'active' : ''}`} aria-label="AI settings" title="AI settings" aria-expanded={settingsOpen} onClick={() => setSettingsOpen((open) => !open)}>⚙</button>
          {embedded && <button className="icon" onClick={close} aria-label="Close Flowbench AI" title="Close">×</button>}
        </div>
      </div>
      {authorized && <div className="ai-head-row ai-model-row">
        <select aria-label="Active model" title="Model" value={model} onChange={(e) => setModel(e.target.value)} disabled={streaming || !models.length}>
          {!models.length && <option value="">No models</option>}
          {models.map((choice) => <option key={choice.slug} value={choice.slug}>{choice.display_name}</option>)}
        </select>
        {!local && <select aria-label="Reasoning effort" title="Reasoning effort: higher levels think longer and use more of the plan's limits" value={effort}
          onChange={(e) => chooseEffort(e.target.value)} disabled={streaming || !models.length}>
          {effortChoices.map((choice) => <option key={choice} value={choice}>{effortLabel(choice)}</option>)}
        </select>}
      </div>}
    </header>

    {settingsOpen && <div className="ai-settings" role="region" aria-label="AI settings panel">
      <ProviderSettings local={local} disabled={busy || streaming} chatgptAccount={account} chatgptConnected={chatgptConnected}
        native={native} onChanged={providerChanged} onSignIn={() => void signIn()} onSignOut={() => void signOut()} />

      <section className="ai-settings-section" aria-label="Permissions">
        <h3>Permissions</h3>
        <label className="ai-consent"><input type="checkbox" checked={fullControl} disabled={busy || !authorized || (streaming && !fullControl)}
          onChange={(e) => setFullControl(e.target.checked)} /><span><b>Full Control for this workspace</b><br />
          <small className="ai-menu-note">{fullControl
            ? 'On for this workspace until Flowbench closes. The AI edits, runs and saves without asking; every edit can be undone.'
            : 'Off: the AI asks before each workflow edit, run and save. Turn on to let it work without prompts. Code and database writes are still confirmed separately.'}</small></span></label>
        {local?.loopback
          ? <p className="ai-menu-note">Data stays on this computer: the model server is on localhost.</p>
          : <label className="ai-consent"><input type="checkbox" checked={routingConsent} disabled={!authorized || consentUpdating || streaming} onChange={(e) => void updateConsent(e.target.checked)} />
            <span>Allow prompts, workflow context and requested data results to be sent to {local ? `the model server at ${local.base_url}` : 'ChatGPT'}</span></label>}
      </section>

      <section className="ai-settings-section" aria-label="Usage">
        <h3>Usage</h3>
        <p className="ai-usage-line">This conversation: <b>{tokens(sessionUsage.input)}</b> tokens sent · <b>{tokens(sessionUsage.output)}</b> received</p>
        <small className="ai-menu-note">{local ? 'Local models cost nothing per token, but a long conversation can outgrow the context length.'
          : 'Each step of a turn resends the conversation, so long tasks add up. Lower reasoning effort and a new conversation per task keep usage down.'}</small>
      </section>

      <WebSearchSettings disabled={busy || streaming} />

      <section className="ai-settings-section" aria-label="Troubleshooting">
        <h3>Troubleshooting</h3>
        <button onClick={() => void loadResponseDiagnostics()} disabled={busy || streaming}>Show safe response diagnostics</button>
        {responseDiagnostics.length > 0 && <div className="ai-response-diagnostics">
          <ul>{responseDiagnostics.slice(-6).reverse().map((entry, i) => {
            const line = diagnosticSummary(entry);
            return <li key={i} className={line.problem ? 'problem' : ''}>{line.text}</li>;
          })}</ul>
          <button className="link" onClick={() => setShowDiagnosticJson((open) => !open)}>{showDiagnosticJson ? 'Hide' : 'Show'} technical details</button>
          {showDiagnosticJson && <><p>Metadata only: prompts, response text, reasoning, tool arguments/results and credentials are excluded.</p>
            <pre>{JSON.stringify(responseDiagnostics, null, 2)}</pre></>}
        </div>}
        {authorized && <div className="ai-settings-row">
          <button onClick={() => void refreshModels()} disabled={busy || streaming}>Refresh models</button>
          {!local && <button onClick={() => void signIn()} disabled={busy || streaming}>Reauthorize</button>}
        </div>}
        <label className="ai-consent"><input type="checkbox" checked={toolsEnabled} disabled={streaming} onChange={(e) => setToolsEnabled(e.target.checked)} />
          <span>Include Flowbench application tools in requests<br /><small className="ai-menu-note">Turn off only to compare a plain answer without tools.</small></span></label>
        <details className="ai-inspect"><summary>Inspect Flowbench yourself</summary>
          <div className="ai-settings-row wrap">{quickActions.map((entry) => <button key={entry.tool} disabled={busy || streaming} onClick={() => void localCommand(entry)}>{entry.label}</button>)}</div>
          <form className="ai-search" onSubmit={(e) => { e.preventDefault(); void inspect({ label: 'Search catalogue', tool: 'catalog.search_tools', args: { query } }); }}>
            <label htmlFor="ai-catalog-search">Search tool catalogue</label>
            <div><input id="ai-catalog-search" type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter, summarize…" />
              <button disabled={!query.trim() || busy}>Search</button></div>
          </form>
          {result !== null && <pre className="ai-local-result">{JSON.stringify(result, null, 2)}</pre>}
        </details>
      </section>
    </div>}

    <nav className="ai-views" role="tablist" aria-label="AI views">
      <button role="tab" aria-selected={view === 'chat'} onClick={() => setView('chat')}>Conversation</button>
      <button role="tab" aria-selected={view === 'task'} onClick={() => void openTasks()}>Task{task?.status === 'running' ? ' ●' : ''}</button>
    </nav>
    {view === 'task' && <section className="ai-task-pane" aria-label="Task activity">
      {task ? <TaskView task={task} /> : <p className="ai-menu-note">No task yet. Ask Flowbench AI to do something and its steps, runs, files and evidence appear here.</p>}
      {recentTasks.filter((t) => t.id !== task?.id).length > 0 && <details className="ai-recent-tasks"><summary>Earlier tasks</summary>
        <ul>{recentTasks.filter((t) => t.id !== task?.id).map((t) => <li key={t.id}><details><summary>
          <span className={`ai-task-status ${t.status}`}>{STATUS_TEXT[t.status] ?? t.status}</span> {t.objective}</summary><TaskView task={t} /></details></li>)}</ul>
      </details>}
    </section>}
    <section className="ai-conversation" aria-label="AI conversation" hidden={view !== 'chat'}>
      <div className="ai-chat-messages" ref={chatScroll} aria-live="polite" onScroll={(e) => {
        const el = e.currentTarget;
        stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 56;
      }}>
        {messages.length === 0 && <div className="ai-empty">
          <div className="ai-orb" aria-hidden="true">✦</div>
          <h2>What would you like to analyse?</h2>
          <p>{fullControl ? 'Flowbench AI can build, validate and run workflows in this workspace.' : 'Flowbench AI can build and validate workflows, asking before each change. Turn on Full Control in settings to let it work without prompts.'}</p>
          <p className="ai-empty-note">Describe the analysis. Flowbench AI finds or downloads the data, builds the workflow on the canvas, runs it and reports what Flowbench computed. Use <b>Add files</b> to point it at your own data.</p>
          {!authorized && <div className="ai-connect-inline">
            <span>{sessionRecovery ? 'Reconnect your protected ChatGPT session to continue.' : connected ? 'ChatGPT plan usage is not authorised yet.' : 'Connect a ChatGPT account, or use a local model with Ollama or LM Studio.'}</span>
            <div className="ai-settings-row center">
              <button className="primary" onClick={() => void signIn()} disabled={busy}>{busy ? 'Opening sign-in…' : 'Continue with ChatGPT'}</button>
              <button onClick={() => setSettingsOpen(true)}>Use a local model</button>
            </div>
          </div>}
          {ready && !routingConsent && <div className="ai-connect-inline">
            <span>Allow Flowbench to send your prompts and the data the AI asks for to {local ? 'the model server' : 'ChatGPT'}.</span>
            <button className="primary" onClick={() => void updateConsent(true)} disabled={consentUpdating}>Allow and start</button>
          </div>}
          {authorized && <div className="ai-examples">{examples.map((example) => <button key={example} disabled={!routingConsent || streaming} onClick={() => void send(example)}>{example}<span>→</span></button>)}</div>}
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
        {actionApproval && <section className="ai-proposal-card" aria-label="Permission request">
          <strong>Allow {actionApproval.permissions.map((p) => p === 'network' ? 'internet access' : p === 'file_writes' ? 'saving files' : p === 'processes' ? 'running programs' : p).join(' and ')} for this task?</strong>
          <pre className="ai-approval-detail">{actionApproval.detail}</pre>
          <small className="ai-menu-note">{actionApproval.permissions.includes('processes')
            ? 'Programs and scripts run with your user account in the workspace folder. You are asked once per task.'
            : 'You are asked once per task. Turn on Full Control in settings to allow downloads and saving files for the workspace without asking.'}</small>
          <div><button className="primary" onClick={() => { useAiConversation.getState().requestActionApproval(null); resolveActionApproval?.(true); resolveActionApproval = null; }}>Allow</button>
            <button onClick={() => { useAiConversation.getState().requestActionApproval(null); resolveActionApproval?.(false); resolveActionApproval = null; }}>Decline</button></div>
        </section>}
        {error && <div className="ai-inline-error" role="alert">{error}<button onClick={() => setError('')}>Dismiss</button></div>}
        {lastProblem?.problem && !streaming && messages.some((m) => m.state === 'failed') && <div className="ai-inline-hint">{lastProblem.text}</div>}
      </div>
      <footer className="ai-composer-wrap">
        {streaming && task && <div className="ai-task-live">
          <TaskView task={task} compact />
          <div className="ai-task-controls">
            <button onClick={togglePause} aria-pressed={paused}>{paused ? 'Resume' : 'Pause'}</button>
            <button onClick={() => void takeOver()} title="Stop the AI and continue by hand">Take over</button>
          </div>
        </div>}
        <div className="ai-contextline"><span className={`routing-dot ${ready ? 'connected' : ''}`} />
          <span title={name}>{name}</span><span>·</span><span>{nodes} nodes</span>
          <span className="ai-tools-mode" title={fullControl ? 'Full Control is on for this workspace until Flowbench closes' : 'The AI asks before edits, runs and saves'}>{fullControl ? 'Full Control' : toolsEnabled ? 'Review mode' : 'No tools'}</span>
          {turnTotal > 0 && <span className={`ai-usage ${turnTotal > HEAVY_TURN_TOKENS || nearContext ? 'heavy' : ''}`}
            title={`This turn: ${turnUsage.input.toLocaleString()} tokens sent, ${turnUsage.output.toLocaleString()} received`}>
            {nearContext ? 'Near the context limit · start a new conversation' : `${tokens(turnTotal)} tokens this turn`}</span>}
        </div>
        {authorized && <form className="ai-compose" onSubmit={(e) => { e.preventDefault(); void send(); }}>
          <button type="button" className="icon ai-attach" aria-label="Add files" title="Add data files: their paths go into your message" onClick={() => void addFiles()} disabled={streaming}>📎</button>
          <textarea aria-label="Message Flowbench AI" rows={2} value={draft} onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(); } }}
            placeholder={routingConsent ? 'Ask about this workflow, or describe the analysis you want…' : 'Allow data sharing to start'} disabled={streaming || !routingConsent} />
          {streaming ? <button type="button" className="ai-stop" onClick={() => void cancel()} aria-label="Stop response">■</button> :
            <button className="primary" aria-label="Send" disabled={consentUpdating || !routingConsent || !draft.trim() || !model}>↑</button>}
        </form>}
        <div className="ai-composer-hint">Enter to send · Shift+Enter for a new line</div>
      </footer>
    </section>
    {busy && <span className="sr-only" role="status">Working…</span>}
  </aside>;
}

// Results reach the model in full. Blob cells (images, charts) are described by size: hex bytes are of
// no use to the model. Use query_results to compute on full outputs inside Flowbench.
export function modelCell(value: string | null, type: string): string | null {
  if (value === null) return null;
  if (type.toLowerCase() === 'blob') return `[blob, ${Math.floor(value.length / 2)} bytes]`;
  return value;
}

function safeRunResults() {
  const run = useEditor.getState().run;
  const ids = run.runId ? { run_id: run.runId } : {};
  if (run.status !== 'succeeded' || !run.result) {
    const failure = (run.result as { error?: { code?: string; message?: string; node_id?: string } } | null)?.error;
    const nodes = useEditor.getState().nodes;
    const ref = (id?: string) => { const i = nodes.findIndex((n) => n.id === id); return i >= 0 ? `node_${i + 1}` : undefined; };
    return { status: run.status, outputs: [], ...ids, ...(failure ? { error: { code: failure.code, message: failure.message, node_id: failure.node_id, node_ref: ref(failure.node_id) } } : {}) };
  }
  const allOutputs = Object.entries(run.result.browse ?? {});
  const outputs = allOutputs.map(([nodeId, preview]) => ({
    node_ref: `node_${useEditor.getState().nodes.findIndex((node) => node.id === nodeId) + 1}`,
    node_id: nodeId,
    fields: preview.fields.map((field) => ({ name: field.name, type: field.type })),
    fields_truncated: false,
    total_rows: preview.total_rows,
    available_rows: preview.rows.length,
    rows: preview.rows.slice(0, 1).map((row) => row.map((cell, i) => modelCell(cell, preview.fields[i].type))),
  }));
  return { status: 'succeeded', outputs, outputs_truncated: false, ...ids };
}

function safeRunResultPage(nodeRef: string, offset: number, limit: number, columns = '') {
  const run = useEditor.getState().run;
  if (run.status !== 'succeeded' || !run.result) throw new Error('No successful workflow result is available');
  const index = Number(nodeRef.match(/^node_(\d+)$/)?.[1] ?? 0) - 1;
  const node = useEditor.getState().nodes[index];
  if (!node) throw new Error('Unknown Browse result node reference');
  const preview = run.result.browse?.[node.id];
  if (!preview) throw new Error('The selected node has no Browse output in the latest run');
  const safeFields = preview.fields.map((field, i) => ({ field, i })).filter(({ field }) =>
    !/password|passwd|secret|token|credential|api.?key|access.?key/i.test(field.name));
  const wanted = columns.split(',').map((name) => name.trim().toLowerCase()).filter(Boolean);
  const unknown = wanted.filter((name) => !safeFields.some(({ field }) => field.name.toLowerCase() === name));
  if (unknown.length) throw new Error(`Unknown column(s): ${unknown.join(', ')}. Use field names from run.get_results.`);
  const chosen = wanted.length ? safeFields.filter(({ field }) => wanted.includes(field.name.toLowerCase())) : safeFields;
  const keep = chosen;
  return {
    node_ref: nodeRef, offset, total_rows: preview.total_rows, available_rows: preview.rows.length,
    fields: keep.map(({ field }) => ({ name: field.name, type: field.type })), fields_truncated: chosen.length > keep.length,
    rows: preview.rows.slice(offset, offset + limit).map((row) =>
      keep.map(({ field, i }) => modelCell(row[i], field.type))),
  };
}

function Message({ message, onRetry }: { message: ChatMessage; onRetry: () => void }) {
  return <article className={`ai-message ${message.role}`} data-state={message.state} data-message-id={message.id}>
    <div className="ai-message-label">{message.role === 'user' ? 'You' : <><span className="ai-assistant-mark">✦</span> Flowbench AI</>}</div>
    {message.text ? <MarkdownText text={message.text} /> : message.state === 'streaming' ? <div className="ai-thinking"><i />Thinking…</div> : null}
    {!!message.activity?.length && <details className="ai-tool-activity" open={message.state === 'streaming' || undefined}><summary>{message.state === 'streaming' || message.activity.length === 1 ? message.activity[message.activity.length - 1] : `${message.activity.length} Flowbench steps`}</summary>
      {message.activity.length > 1 && <ul>{message.activity.map((activity, i) => <li key={`${i}_${activity}`}>{activity}</li>)}</ul>}</details>}
    {message.verification?.status === 'verified' && <div className="ai-verification ok" title={message.verification.runIds?.length ? `Run ${message.verification.runIds.join(', ')}` : undefined}>✓ Backed by a Flowbench run</div>}
    {message.verification?.status === 'unverified' && <div className="ai-verification warn" role="note">Not verified: no Flowbench run backs this answer. Treat its numbers as unconfirmed.</div>}
    {message.verification?.status === 'incomplete' && <div className="ai-verification warn" role="note">Incomplete: {message.verification.missing?.length ? `missing ${message.verification.missing.join('; ')}` : 'some requested deliverables are missing'}. See the Task tab.</div>}
    {message.verification?.status === 'answered_without_execution' && <div className="ai-verification" role="note">Answered without running Flowbench{message.verification.note ? `: ${message.verification.note}` : ''}</div>}
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
type TaskRecord = { verification?: Verification['status']; classified_as_question?: string | null; evidence?: Array<{ run_id?: string | null }>; missing?: string[] };
function verificationOf(task: TaskRecord | null | undefined): Verification | undefined {
  if (!task?.verification) return undefined;
  const runIds = [...new Set((task.evidence ?? []).map((e) => e.run_id).filter((id): id is string => Boolean(id)))];
  return { status: task.verification, note: task.classified_as_question ?? undefined, runIds, missing: task.missing ?? [] };
}
/** Where the AI's web_search goes: the user's SearXNG instance, else DuckDuckGo. Stored by the engine. */
function WebSearchSettings({ disabled }: { disabled: boolean }) {
  const [url, setUrl] = useState('');
  const [engines, setEngines] = useState('');
  const [note, setNote] = useState<{ text: string; ok: boolean } | null>(null);
  const [working, setWorking] = useState(false);
  useEffect(() => {
    engine().request<{ searxng_url: string | null; searxng_engines?: string | null }>('agent.search_settings', {})
      .then((s) => { setUrl(s.searxng_url ?? ''); setEngines(s.searxng_engines ?? ''); }).catch(() => undefined);
  }, []);
  async function save(test: boolean) {
    setWorking(true); setNote(null);
    try {
      const s = await engine().request<{ searxng_url: string | null; test?: { results: number; unresponsive_engines: unknown[] } }>(
        'agent.search_settings', { searxng_url: url.trim() || null, searxng_engines: engines.trim() || null, test });
      setNote(!s.searxng_url ? { text: 'Web search uses DuckDuckGo.', ok: true }
        : s.test ? { text: `SearXNG answered with ${s.test.results} result(s)${s.test.unresponsive_engines.length ? `; engines not responding: ${s.test.unresponsive_engines.map((e) => (e as string[])[0]).join(', ')}` : ''}.`, ok: s.test.results > 0 }
        : { text: 'Saved.', ok: true });
    } catch (e) { setNote({ text: messageOf(e), ok: false }); }
    finally { setWorking(false); }
  }
  return <section className="ai-settings-section" aria-label="Web search">
    <h3>Web search</h3>
    <div className="ai-settings-body">
      <label>SearXNG address
        <input value={url} placeholder="http://localhost:8888" onChange={(e) => setUrl(e.target.value)} disabled={disabled || working} spellCheck={false} /></label>
      <label>Engines (optional)
        <input value={engines} placeholder="yandex,bing" onChange={(e) => setEngines(e.target.value)} disabled={disabled || working} spellCheck={false} /></label>
      <small className="ai-menu-note">Your own SearXNG instance (JSON output enabled). Many engines block self-hosted instances; list the ones that answer, or leave empty for SearXNG's defaults. If SearXNG finds nothing, DuckDuckGo is used. Official statistics are also found through GOV.UK and data.gov.uk search.</small>
      <div className="ai-settings-row wrap">
        <button onClick={() => void save(true)} disabled={disabled || working || !url.trim()}>{working ? 'Testing…' : 'Save and test'}</button>
        <button onClick={() => void save(false)} disabled={disabled || working}>Save</button>
      </div>
    </div>
    {note && <p className={`ai-settings-note ${note.ok ? 'ok' : 'err'}`} role={note.ok ? 'status' : 'alert'}>{note.text}</p>}
  </section>;
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
