import { describe, expect, it } from 'vitest';
import { dispatchAgentTool, AGENT_PROTOCOL, type AgentContext } from './protocol';
import type { CanvasNode } from '../model/workflow';

function context(): AgentContext {
  const node = { id: 'input_1', type: 'tool', position: { x: 0, y: 0 }, data: { kind: 'tool', tool: 'core.text_input',
    config: { rows: [{ Customer: 'Sensitive customer value' }], password: 'private-secret', file_path: 'C:/private/data.csv' } } } as CanvasNode;
  return { tools: {}, catalog: [{ id: 'input:text', name: 'Text Input', category: 'In/Out', implementation_status: 'implemented',
    verification_status: 'partial', kernel: 'core.text_input', documentation_url: null, external_prerequisites_blocking: false, unavailable_reason: null }],
    engineInfo: { version: 'test', python: '3.12', protocol: 2 }, engineError: null, nodes: [node], edges: [], name: 'Sales',
    description: 'Fixture', profile: 'test-profile', problems: [{ code: 'bad-config', message: 'password=private-secret', node_id: 'input_1' }],
    schemas: {}, validating: false, run: { runId: null, status: 'idle', events: [], nodeStatus: {}, nodeRows: {}, nodeMs: {}, result: null, startedAt: null },
    history: [], selectedResult: null };
}

describe('flowbench.agent.v1 read-only tools', () => {
  const env = { getState: context, validate: async () => undefined };

  it('provides explicit protocol and evidence-based capability state', async () => {
    expect(AGENT_PROTOCOL).toBe('flowbench.agent.v1');
    await expect(dispatchAgentTool(env, 'app.get_capabilities')).resolves.toMatchObject({ catalogue_entries: 1, installed_kernels: 0, implemented_catalogue_entries: 1 });
  });

  it('returns workflow graph structure but never configuration values or data rows', async () => {
    const output = JSON.stringify(await dispatchAgentTool(env, 'workflow.get_snapshot'));
    expect(output).toContain('core.text_input');
    expect(output).toContain('configured_options');
    expect(output).not.toContain('Sensitive customer value');
    expect(output).not.toContain('private-secret');
    expect(output).not.toContain('C:/private/data.csv');
  });

  it('strictly rejects unknown tools and unregistered write operations', async () => {
    await expect(dispatchAgentTool(env, 'workflow.delete_node', { node_id: 'input_1' })).rejects.toThrow('Unknown or unavailable');
    await expect(dispatchAgentTool(env, 'workflow.get_snapshot', { include_rows: true })).rejects.toThrow('Unexpected argument');
  });

  it('validates required argument types before catalogue access', async () => {
    await expect(dispatchAgentTool(env, 'catalog.search_tools')).rejects.toThrow('Missing argument: query');
    await expect(dispatchAgentTool(env, 'catalog.search_tools', { query: 4 })).rejects.toThrow('must be a string');
    await expect(dispatchAgentTool(env, 'catalog.search_tools', { query: 'text', extra: true })).rejects.toThrow('Unexpected argument');
  });

  it('redacts secret-like text in diagnostics', async () => {
    const result = JSON.stringify(await dispatchAgentTool(env, 'workflow.get_diagnostics'));
    expect(result).toContain('password=[REDACTED]');
    expect(result).not.toContain('private-secret');
  });

  it('runs validation only through the existing validator', async () => {
    let calls = 0;
    await dispatchAgentTool({ ...env, validate: async () => { calls += 1; } }, 'workflow.validate');
    expect(calls).toBe(1);
  });
});
