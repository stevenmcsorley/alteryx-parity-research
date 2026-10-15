import { describe, expect, it, vi } from 'vitest';
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

describe('flowbench.agent.v1 application tools', () => {
  const env = { getState: context, validate: async () => undefined };

  it('provides explicit protocol and evidence-based capability state', async () => {
    expect(AGENT_PROTOCOL).toBe('flowbench.agent.v1');
    await expect(dispatchAgentTool(env, 'app.get_capabilities')).resolves.toMatchObject({ catalogue_entries: 1, installed_kernels: 0, implemented_catalogue_entries: 1 });
  });

  it('returns workflow graph structure but never configuration values or data rows', async () => {
    const output = JSON.stringify(await dispatchAgentTool(env, 'workflow.get_snapshot'));
    expect(output).toContain('input:text');
    expect(output).toContain('node_1');
    expect(output).not.toContain('Sensitive customer value');
    expect(output).not.toContain('private-secret');
    expect(output).not.toContain('C:/private/data.csv');
    expect(output).not.toContain('Fixture');
    expect(output).not.toContain('Sales');
    expect(output).not.toContain('test-profile');
  });

  it('exposes actionable node configuration while excluding credentials and raw row data', async () => {
    const result: any = await dispatchAgentTool(env, 'workflow.get_node_config', { node_ref: 'node_1' });
    expect(result).toMatchObject({ node_ref: 'node_1', kind: 'tool', tool_id: 'core.text_input', config: { file_path: 'C:/private/data.csv' } });
    expect(JSON.stringify(result)).not.toContain('private-secret');
    expect(JSON.stringify(result)).not.toContain('Sensitive customer value');
    expect(JSON.stringify(result)).toContain('C:/private/data.csv');
  });

  it('exposes safe Interface question and Condition configuration with stable control-anchor aliases', async () => {
    const question = { id: 'enabled', prompt: 'Run the optional filter?', type: 'boolean', default: true };
    const action = { id: 'question_1', type: 'applicationAction', position: { x: 0, y: 0 }, data: {
      kind: 'application-action', question, config_path: ['expression'] } } as CanvasNode;
    const condition = { id: 'condition_1', type: 'applicationCondition', position: { x: 100, y: 0 }, data: {
      kind: 'application-condition', when: { question: 'enabled', operator: 'truthy' } } } as CanvasNode;
    const state = { ...context(), nodes: [action, condition], edges: [{ id: 'e', source: 'condition_1', sourceHandle: 'True Condition',
      target: 'question_1', targetHandle: 'Condition' }] } as AgentContext;
    const env = { ...({ getState: () => state, validate: async () => undefined }) };
    expect(await dispatchAgentTool(env, 'workflow.get_node_config', { node_ref: 'node_1' })).toMatchObject({
      kind: 'application-action', config: { question: { id: 'enabled', type: 'boolean', default: true }, config_path: ['expression'] },
    });
    expect(await dispatchAgentTool(env, 'workflow.get_node_config', { node_ref: 'node_2' })).toMatchObject({
      kind: 'application-condition', config: { condition: { question: 'enabled', operator: 'truthy' } },
    });
    expect(await dispatchAgentTool(env, 'workflow.get_snapshot')).toMatchObject({
      nodes: [{ tool_id: 'interface:action' }, { tool_id: 'interface:condition' }],
      edges: [{ source_anchor: 'output_1', target_anchor: 'input_1' }],
    });
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

  it('excludes diagnostic messages from validation and diagnostics responses', async () => {
    const validation = JSON.stringify(await dispatchAgentTool(env, 'workflow.validate'));
    const result = JSON.stringify(await dispatchAgentTool(env, 'workflow.get_diagnostics'));
    expect(validation).toContain('bad-config');
    expect(validation).not.toContain('private-secret');
    expect(validation).not.toContain('password=');
    expect(result).not.toContain('private-secret');
    expect(result).not.toContain('password=');
  });

  it('projects tool manifests without descriptive metadata, defaults, or user-entered values', async () => {
    const tool: any = { tool_id: 'core.filter', version: '1', catalog_ids: ['preparation:filter'], display_name: 'Filter',
      description: 'Potentially private tool description', side_effects: 'none', deterministic: true, dependencies: [],
      inputs: [{ name: 'input', label: 'Private anchor label', required: true, multiple: false, description: 'private' }],
      outputs: [{ name: 'true', label: 'Private output label', required: false, multiple: false, description: '' }],
      config_schema: { type: 'object', description: 'private schema note', properties: { expression: { type: 'string', default: 'private query value', description: 'private' } } } };
    const projected = JSON.stringify(await dispatchAgentTool({ ...env, getState: () => ({ ...context(), tools: { 'core.filter': tool } }) }, 'catalog.get_tool_manifest', { tool_id: 'core.filter' }));
    expect(projected).toContain('core.filter');
    expect(projected).not.toContain('Potentially private');
    expect(projected).not.toContain('Private anchor');
    expect(projected).not.toContain('private query value');
    expect(projected).not.toContain('private schema note');
  });

  it('does not return private catalogue labels or descriptions', async () => {
    const result = JSON.stringify(await dispatchAgentTool(env, 'catalog.search_tools', { query: 'input' }));
    expect(result).toContain('input:text');
    expect(result).not.toContain('Text Input');
    expect(result).not.toContain('Fixture');
  });

  it('runs validation only through the existing validator', async () => {
    let calls = 0;
    await dispatchAgentTool({ ...env, validate: async () => { calls += 1; } }, 'workflow.validate');
    expect(calls).toBe(1);
  });

  it('keeps snapshot, validate and diagnostics problem summaries in the shared result shape', async () => {
    const state = context();
    const local = { ...env, getState: () => state, validate: async () => undefined };
    const snapshot: any = await dispatchAgentTool(local, 'workflow.get_snapshot');
    const validation: any = await dispatchAgentTool(local, 'workflow.validate');
    const diagnostics: any = await dispatchAgentTool(local, 'workflow.get_diagnostics');
    expect(snapshot.problems).toEqual({ count: 1, problems: [{ code: 'bad-config', node_ref: 'node_1' }] });
    expect(validation).toEqual({ count: 1, problems: [{ code: 'bad-config', node_ref: 'node_1' }] });
    expect(diagnostics.problems).toEqual(validation);
  });

  it('matches the shared contracts for disconnected status and already-running execution', async () => {
    expect(await dispatchAgentTool({ ...env, getState: () => ({ ...context(), engineInfo: null }) }, 'app.get_status'))
      .toMatchObject({ engine: { connected: false, version: 'unavailable' } });
    expect(await dispatchAgentTool({ ...env, requestRun: () => ({ status: 'already_running', outputs: [] }) }, 'workflow.request_run'))
      .toEqual({ status: 'already_running', outputs: [] });
  });

  it('routes result-page requests without an artificial page ceiling', async () => {
    const page = { node_ref: 'node_1', offset: 20, total_rows: 1000, available_rows: 500, fields: [], fields_truncated: false, rows: [] };
    const getResultPage = vi.fn((_nodeRef: string, offset: number) => ({ ...page, offset }));
    await expect(dispatchAgentTool({ ...env, getResultPage }, 'run.get_result_page', { node_ref: 'node_1', offset: 20, limit: 25 })).resolves.toEqual(page);
    expect(getResultPage).toHaveBeenCalledWith('node_1', 20, 25);
    await expect(dispatchAgentTool({ ...env, getResultPage }, 'run.get_result_page', { node_ref: 'node_1', offset: 501, limit: 25 })).resolves.toEqual({ ...page, offset: 501 });
    expect(getResultPage).toHaveBeenLastCalledWith('node_1', 501, 25);
  });

  it('routes a workflow patch only to the isolated proposal callback', async () => {
    const proposePatch = vi.fn(async (_patch: string, _revision: string) => ({ status: 'approved', operation_count: 1, valid: true, problem_codes: [] }));
    const patch = JSON.stringify({ operations: [{ op: 'remove', node_ref: 'node_1' }] });
    await expect(dispatchAgentTool({ ...env, proposePatch }, 'workflow.propose_patch', { patch_json: patch, base_revision: 'rev_12345678' }))
      .resolves.toEqual({ status: 'approved', operation_count: 1, valid: true, problem_codes: [] });
    expect(proposePatch).toHaveBeenCalledWith(patch, 'rev_12345678');
    await expect(dispatchAgentTool(env, 'workflow.propose_patch', { patch_json: patch, base_revision: 'rev_12345678' }))
      .rejects.toThrow('unavailable in this application context');
  });

  it('exposes field names and types without sample values or sensitive fields', async () => {
    const state = context();
    state.schemas = { input_1: { output: [
      { name: 'Sales', type: 'fixed_decimal', size: 12, scale: 2 },
      { name: 'api_token', type: 'v_wstring' },
    ] } };
    const result = await dispatchAgentTool({ ...env, getState: () => state }, 'workflow.get_schema', { node_ref: 'node_1' });
    expect(result).toEqual({ node_ref: 'node_1', anchors: [{ anchor: 'output', anchor_name: 'output', fields: [{ name: 'Sales', type: 'fixed_decimal', size: 12, scale: 2 }] }] });
  });
});
