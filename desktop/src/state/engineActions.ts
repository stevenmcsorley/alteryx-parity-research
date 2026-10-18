// Side-effecting operations against the engine. Components call these; the store holds results.
import { createTransport, EngineRpcError, type EngineTransport } from '../engine/transport';
import type { CatalogEntry, HistoryEntry, Profile, RunPayload, ToolManifest, ValidateResult } from '../engine/types';
import { fromDocument, toDocument } from '../model/workflow';
import { graphOf, useEditor } from './store';
import { promptApplication } from '../ui/dialogs';
import { notify } from '../ui/toast';

let transport: EngineTransport | null = null;
let unsubscribe: (() => void) | null = null;

export function engine(): EngineTransport {
  if (!transport) transport = createTransport();
  return transport;
}

/** Test hook: replace the transport (unit tests use an in-memory fake). */
export function setTransport(t: EngineTransport) {
  unsubscribe?.();
  unsubscribe = null;
  transport = t;
}

export async function initEngine(): Promise<void> {
  const st = useEditor.getState();
  try {
    const t = engine();
    const info = await t.request<{ version: string; python: string; protocol: number; cwd: string; default_workspace?: string }>('engine.info');
    const [tools, catalog, profiles] = await Promise.all([
      t.request<ToolManifest[]>('tools.list'),
      t.request<CatalogEntry[]>('catalog.list'),
      t.request<Profile[]>('profiles.list'),
    ]);
    st.setEngineMeta({ engineInfo: info, tools: Object.fromEntries(tools.map((m) => [m.tool_id, m])), catalog, profiles,
      engineError: null });
    // Untitled workflows live in a folder the user owns (~/Flowbench in the desktop app), not the install folder.
    if (!useEditor.getState().baseDir) useEditor.getState().setFilePath(useEditor.getState().filePath, info.default_workspace || info.cwd);
    unsubscribe?.();
    unsubscribe = t.subscribe((n) => {
      if (n.method === 'run.event') useEditor.getState().runEvent(n.params);
      if (n.method === 'run.finished') void collectResult(n.params.run_id);
    });
    void refreshHistory();
  } catch (e) {
    st.setEngineMeta({ engineError: describe(e) });
  }
}

export function describe(e: unknown): string {
  if (e instanceof EngineRpcError) return e.data?.message ?? e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

let validateTimer: ReturnType<typeof setTimeout> | null = null;
let validateSeq = 0;

export function scheduleValidate(delay = 350) {
  if (validateTimer) clearTimeout(validateTimer);
  validateTimer = setTimeout(() => void validateNow(), delay);
}

export async function validateNow(): Promise<ValidateResult | null> {
  const s = useEditor.getState();
  if (!s.engineInfo) return null;
  const seq = ++validateSeq;
  s.setValidating(true);
  try {
    const res = await engine().request<ValidateResult>('workflow.validate', {
      workflow: toDocument(graphOf(s)), base_dir: s.baseDir, profile: s.profile });
    if (seq === validateSeq) useEditor.getState().setValidation(res.problems, res.schemas);
    return res;
  } catch (e) {
    if (seq === validateSeq) useEditor.getState().setValidation([{ code: 'engine-error', message: describe(e), node_id: null }], {});
    return null;
  }
}

export async function runWorkflow(options: { workers?: number; batch_rows?: number | null } = {}): Promise<void> {
  const s = useEditor.getState();
  if (s.run.status === 'running') return;
  const document = toDocument(graphOf(s));
  const emptyEffects: WorkflowEffects = { complete: false, file_writes: [], database_writes: [], processes: [], network_requests: [], remote_writes: [],
    unresolved: [{ path: [], reason: 'permission scan failed' }] };
  let effects: WorkflowEffects = emptyEffects;
  try {
    effects = await engine().request<WorkflowEffects>('workflow.permissions', { workflow: document, base_dir: s.baseDir });
  } catch {
    // Fail closed. A failed preflight cannot authorize any workflow-wide capability.
  }
  if (!(await offerDownloads(effects.downloads ?? []))) return;
  const codeTools = effects.processes;
  let allow = false;
  if (codeTools.length) {
    allow = window.confirm(`This workflow contains ${codeTools.length} tool(s) that run code or programs on this computer `
      + `(${codeTools.map(effectLocation).join(', ')}). Only allow this for workflows you trust. Allow for this run?`)
      && effects.complete;
    if (!allow) return;
  }
  const dbWriters = effects.database_writes;
  let allowDatabaseWrites = false;
  if (dbWriters.length) {
    allowDatabaseWrites = window.confirm(`This workflow can write to a database (${dbWriters.map(effectLocation).join(', ')}). `
      + 'Continue only if the selected connection is configured for writes and you trust the workflow. Allow database writes for this run?')
      && effects.complete;
    if (!allowDatabaseWrites) return;
  }
  const blobWriters = effects.file_writes;
  let allowFileWrites = false;
  if (blobWriters.length) {
    allowFileWrites = window.confirm(`This workflow writes files (${blobWriters.map(effectLocation).join(', ')}). `
      + 'Review the configured destination and file naming options. Allow these file writes for this run?')
      && effects.complete;
    if (!allowFileWrites) return;
  }
  const networkTools = effects.network_requests ?? [];
  let allowNetwork = false;
  if (networkTools.length) {
    allowNetwork = window.confirm(`This workflow sends web requests (${networkTools.map(effectLocation).join(', ')}). `
      + 'Requests can include record data and reach any address in the workflow. Allow network requests for this run?')
      && effects.complete;
    if (!allowNetwork) return;
  }
  const remoteWriters = effects.remote_writes ?? [];
  let allowRemoteWrites = false;
  if (remoteWriters.length) {
    allowRemoteWrites = window.confirm(`This workflow pushes changes or sends email to a remote service (${remoteWriters.map(effectLocation).join(', ')}). `
      + 'Check the targets (repository and branch, or email recipients). Allow remote writes for this run?')
      && effects.complete;
    if (!allowRemoteWrites) return;
  }
  if (!effects.complete && effects.unresolved.length) {
    window.alert('Flowbench could not inspect every nested macro. File, database, process, network and remote-write permissions remain disabled for this run.');
  }
  try {
    const app = await engine().request<{ questions: import('../ui/dialogs').AppQuestion[] }>('workflow.application', { workflow: document, base_dir: s.baseDir });
    const applicationValues = app.questions.length ? await promptApplication(app.questions) : undefined;
    if (app.questions.length && applicationValues === null) return;
    const res = await engine().request<{ run_id: string; status: string; problems?: any[] }>('run.start', {
      workflow: document, base_dir: s.baseDir, profile: s.profile, preview_rows: 500,
      workers: options.workers ?? 2, allow_processes: allow,
      allow_database_writes: allowDatabaseWrites,
      allow_file_writes: allowFileWrites,
      allow_network: allowNetwork,
      allow_remote_writes: allowRemoteWrites,
      ...(applicationValues ? { application_values: applicationValues } : {}),
      ...(options.batch_rows ? { batch_rows: options.batch_rows } : {}) });
    if (res.status === 'invalid') {
      useEditor.getState().runInvalid(res.problems ?? []);
      return;
    }
    useEditor.getState().runStarted(res.run_id);
    // In case the finished notification raced ahead of runStarted, poll once shortly after.
    setTimeout(() => {
      const r = useEditor.getState().run;
      if (r.runId === res.run_id && r.status === 'running') void collectResult(res.run_id, true);
    }, 1500);
  } catch (e) {
    useEditor.getState().runInvalid([{ code: 'engine-error', message: describe(e), node_id: null }]);
  }
}

type Download = { id: string; title: string; size_mb: number; source: string; nodes: string[] };

/** Components that download on first use: ask once, install, then let the run continue. */
async function offerDownloads(downloads: Download[]): Promise<boolean> {
  if (!downloads.length) return true;
  const total = downloads.reduce((sum, d) => sum + d.size_mb, 0);
  const ok = window.confirm('This workflow needs components that download on first use:\n'
    + downloads.map((d) => `• ${d.title}: about ${d.size_mb} MB from ${d.source} (for ${d.nodes.join(', ')})`).join('\n')
    + `\n\nAbout ${total} MB in total. They are installed once in your Flowbench folder and reused. Download now?`);
  if (!ok) return false;
  notify(`Downloading ${downloads.length} component(s), about ${total} MB. This can take a few minutes…`);
  try {
    await engine().request('addons.install', { components: downloads.map((d) => d.id) });
    notify('Components installed.');
    return true;
  } catch (e) {
    notify(`Download failed: ${describe(e)}`);
    return false;
  }
}

type WorkflowEffects = {
  downloads?: Download[];
  complete: boolean;
  file_writes: Array<{ node_id: string; tool_id: string; path: string[] }>;
  database_writes: Array<{ node_id: string; tool_id: string; path: string[] }>;
  processes: Array<{ node_id: string; tool_id: string; path: string[] }>;
  network_requests?: Array<{ node_id: string; tool_id: string; path: string[] }>;
  remote_writes?: Array<{ node_id: string; tool_id: string; path: string[] }>;
  unresolved: Array<{ path: string[]; reason: string }>;
};

function effectLocation(effect: { node_id: string; path: string[] }): string {
  return effect.path.length > 1 ? `${effect.node_id} (through ${effect.path.slice(0, -1).join(' → ')})` : effect.node_id;
}

async function collectResult(runId: string, poll = false): Promise<void> {
  const r = await engine().request<{ status: string; result: RunPayload | null }>('run.result', { run_id: runId });
  if (r.status === 'running') {
    if (poll) setTimeout(() => void collectResult(runId, true), 500);
    return;
  }
  if (r.result) useEditor.getState().runFinished({ ...r.result, run_id: runId });
  void refreshHistory();
}

export async function cancelRun(): Promise<void> {
  const r = useEditor.getState().run;
  if (r.runId && r.status === 'running') await engine().request('run.cancel', { run_id: r.runId });
}

export async function refreshHistory(): Promise<void> {
  try {
    useEditor.getState().setHistory(await engine().request<HistoryEntry[]>('history.list', { limit: 30 }));
  } catch {
    /* history optional */
  }
}

function dirOf(p: string): string {
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  return i >= 0 ? p.slice(0, i) : '';
}

export async function saveWorkflow(path: string): Promise<void> {
  if (!path.endsWith('.flow.json')) path += '.flow.json';
  const s = useEditor.getState();
  await engine().request('file.write_text', { path, text: JSON.stringify(toDocument(graphOf(s)), null, 2) + '\n' });
  useEditor.getState().setFilePath(path, dirOf(path));
  useEditor.getState().markSaved();
  scheduleValidate(0);
}

const IMPORTABLE = /\.(yxmd|yxmc|yxwz|yxzp)$/i;
export async function openWorkflow(path: string): Promise<void> {
  if (IMPORTABLE.test(path)) {
    const res = await engine().request<{ workflow: any; report: any; base_dir: string }>('workflow.import', { path });
    useEditor.getState().loadGraph(fromDocument(res.workflow), null, res.base_dir);
    useEditor.setState({ importReport: res.report, dirty: true, bottomTab: 'messages' });
    scheduleValidate(0);
    return;
  }
  const res = await engine().request<{ path: string; text: string }>('file.read_text', { path });
  const doc = JSON.parse(res.text);
  useEditor.getState().loadGraph(fromDocument(doc), res.path, dirOf(res.path));
  scheduleValidate(0);
}

export async function exportYxmd(path: string): Promise<{ written: boolean; problems: string[] }> {
  const s = useEditor.getState();
  return engine().request('workflow.export', { workflow: toDocument(graphOf(s)), path });
}

export async function exportText(path: string, text: string): Promise<string> {
  const res = await engine().request<{ path: string }>('file.write_export', { path, text });
  return res.path;
}

export async function exportRunResult(runId: string, node: string, anchor: string,
  path: string, format: 'csv' | 'parquet'): Promise<string> {
  const res = await engine().request<{ path: string }>('run.export', {
    run_id: runId, node_id: node, anchor, path, format });
  return res.path;
}

export function previewToCsv(fields: { name: string }[], rows: (string | null)[][]): string {
  const esc = (v: string | null) => (v === null ? '' : /[",\r\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  return [fields.map((f) => esc(f.name)).join(','), ...rows.map((r) => r.map(esc).join(','))].join('\n') + '\n';
}
