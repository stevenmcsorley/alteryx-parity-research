// Side-effecting operations against the engine. Components call these; the store holds results.
import { createTransport, EngineRpcError, type EngineTransport } from '../engine/transport';
import type { CatalogEntry, HistoryEntry, Profile, RunPayload, ToolManifest, ValidateResult } from '../engine/types';
import { fromDocument, toDocument } from '../model/workflow';
import { graphOf, useEditor } from './store';

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
    const info = await t.request<{ version: string; python: string; protocol: number; cwd: string }>('engine.info');
    const [tools, catalog, profiles] = await Promise.all([
      t.request<ToolManifest[]>('tools.list'),
      t.request<CatalogEntry[]>('catalog.list'),
      t.request<Profile[]>('profiles.list'),
    ]);
    st.setEngineMeta({ engineInfo: info, tools: Object.fromEntries(tools.map((m) => [m.tool_id, m])), catalog, profiles,
      engineError: null });
    if (!useEditor.getState().baseDir) useEditor.getState().setFilePath(useEditor.getState().filePath, info.cwd);
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
  try {
    const res = await engine().request<{ run_id: string; status: string; problems?: any[] }>('run.start', {
      workflow: toDocument(graphOf(s)), base_dir: s.baseDir, profile: s.profile, preview_rows: 500,
      workers: options.workers ?? 2, ...(options.batch_rows ? { batch_rows: options.batch_rows } : {}) });
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

export async function openWorkflow(path: string): Promise<void> {
  const res = await engine().request<{ path: string; text: string }>('file.read_text', { path });
  const doc = JSON.parse(res.text);
  useEditor.getState().loadGraph(fromDocument(doc), res.path, dirOf(res.path));
  scheduleValidate(0);
}

export async function exportText(path: string, text: string): Promise<string> {
  const res = await engine().request<{ path: string }>('file.write_export', { path, text });
  return res.path;
}

export function previewToCsv(fields: { name: string }[], rows: (string | null)[][]): string {
  const esc = (v: string | null) => (v === null ? '' : /[",\r\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  return [fields.map((f) => esc(f.name)).join(','), ...rows.map((r) => r.map(esc).join(','))].join('\n') + '\n';
}
