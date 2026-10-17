import { afterEach, describe, expect, it, vi } from 'vitest';
import { runWorkflow, setTransport } from './engineActions';
import { useEditor } from './store';

const effects = (network: number, complete = true, remote = 0) => ({ complete, file_writes: [], database_writes: [], processes: [],
  network_requests: Array.from({ length: network }, (_, i) => ({ node_id: `dl${i}`, tool_id: 'core.download', path: [`dl${i}`] })),
  remote_writes: Array.from({ length: remote }, (_, i) => ({ node_id: `gh${i}`, tool_id: 'core.github_commit', path: [`gh${i}`] })),
  unresolved: complete ? [] : [{ path: ['m'], reason: 'x' }] });

function stubEngine(scan: unknown) {
  const calls: Array<{ method: string; params: any }> = [];
  setTransport({ request: async (method: string, params: any) => {
    calls.push({ method, params });
    if (method === 'workflow.permissions') return scan;
    if (method === 'workflow.application') return { questions: [] };
    if (method === 'run.start') return { run_id: 'r1', status: 'running' };
    return {};
  }, subscribe: () => () => {} } as any);
  return calls;
}

describe('run permissions', () => {
  afterEach(() => { vi.unstubAllGlobals(); useEditor.getState().newWorkflow(); });

  it('offers first-use downloads, installs them, then runs', async () => {
    for (const answer of [true, false]) {
      useEditor.getState().newWorkflow();
      const confirm = vi.fn(() => answer);
      vi.stubGlobal('window', { ...window, confirm, alert: vi.fn() });
      const calls = stubEngine({ ...effects(0), downloads: [
        { id: 'language-pack', title: 'Flowbench language pack', size_mb: 120, source: 'pypi.org', nodes: ['ner'] },
        { id: 'spacy-model:english', title: 'English language model', size_mb: 13, source: 'github.com/explosion', nodes: ['ner'] }] });
      await runWorkflow();
      expect(confirm).toHaveBeenCalledWith(expect.stringContaining('About 133 MB in total'));
      const install = calls.find((c) => c.method === 'addons.install');
      if (answer) {
        expect(install?.params.components).toEqual(['language-pack', 'spacy-model:english']);
        expect(calls.findIndex((c) => c.method === 'addons.install')).toBeLessThan(calls.findIndex((c) => c.method === 'run.start'));
      } else {
        expect(install).toBeUndefined();
        expect(calls.find((c) => c.method === 'run.start')).toBeUndefined();
      }
    }
  });

  it('asks before network requests and passes the answer to the engine', async () => {
    for (const answer of [true, false]) {
      useEditor.getState().newWorkflow();
      const confirm = vi.fn(() => answer);
      vi.stubGlobal('window', { ...window, confirm, alert: vi.fn() });
      const calls = stubEngine(effects(1));
      await runWorkflow();
      expect(confirm).toHaveBeenCalledWith(expect.stringContaining('sends web requests (dl0)'));
      const start = calls.find((c) => c.method === 'run.start');
      if (answer) expect(start?.params.allow_network).toBe(true);
      else expect(start).toBeUndefined();
    }
  });

  it('asks separately before remote writes and sends both grants', async () => {
    const confirm = vi.fn(() => true);
    vi.stubGlobal('window', { ...window, confirm, alert: vi.fn() });
    const calls = stubEngine(effects(1, true, 1));
    await runWorkflow();
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('pushes changes or sends email to a remote service (gh0)'));
    expect(calls.find((c) => c.method === 'run.start')?.params).toMatchObject({ allow_network: true, allow_remote_writes: true });
    useEditor.getState().newWorkflow();
    const declineRemote = vi.fn((text: string) => !text.includes('remote service'));
    vi.stubGlobal('window', { ...window, confirm: declineRemote, alert: vi.fn() });
    const declined = stubEngine(effects(1, true, 1));
    await runWorkflow();
    expect(declined.find((c) => c.method === 'run.start')).toBeUndefined();
  });

  it('never grants network access when the scan is incomplete or there is nothing to ask', async () => {
    vi.stubGlobal('window', { ...window, confirm: vi.fn(() => true), alert: vi.fn() });
    let calls = stubEngine(effects(1, false));
    await runWorkflow();
    expect(calls.find((c) => c.method === 'run.start')).toBeUndefined();
    useEditor.getState().newWorkflow();
    calls = stubEngine(effects(0));
    await runWorkflow();
    expect(calls.find((c) => c.method === 'run.start')?.params.allow_network).toBe(false);
  });
});
