import { afterEach, describe, expect, it, vi } from 'vitest';
import { runWorkflow, setTransport } from './engineActions';
import { useEditor } from './store';

const effects = (network: number, complete = true) => ({ complete, file_writes: [], database_writes: [], processes: [],
  network_requests: Array.from({ length: network }, (_, i) => ({ node_id: `dl${i}`, tool_id: 'core.download', path: [`dl${i}`] })),
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
