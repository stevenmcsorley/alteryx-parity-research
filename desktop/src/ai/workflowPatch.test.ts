import { afterEach, describe, expect, it } from 'vitest';
import { prepareWorkflowProposal, workflowRevision } from './workflowPatch';
import { useEditor } from '../state/store';
import { setTransport } from '../state/engineActions';
import { toDocument } from '../model/workflow';

const emptyGraph = { nodes: [], edges: [], name: 'Untitled workflow' };
const manifest = (tool_id: string, inputs: string[], outputs: string[]) => ({ tool_id, version: '1', display_name: tool_id,
  catalog_ids: [], inputs: inputs.map((name) => ({ name, label: name, required: name === 'input', multiple: false, description: '' })),
  outputs: outputs.map((name) => ({ name, label: name, required: false, multiple: false, description: '' })),
  config_schema: { type: 'object' }, side_effects: 'none', deterministic: true, dependencies: [], description: '' });

afterEach(() => { setTransport(null as any); useEditor.getState().newWorkflow(); });

describe('AI workflow patch staging', () => {
  it('validates a candidate against the engine while leaving the editor untouched', async () => {
    useEditor.getState().loadGraph(emptyGraph as any);
    useEditor.getState().setEngineMeta({ tools: {
      'core.input_data': manifest('core.input_data', [], ['output']) as any,
      'core.filter': manifest('core.filter', ['input'], ['true', 'false']) as any,
    } });
    let checked: any;
    setTransport({ request: async (method: string, params: any) => { expect(method).toBe('workflow.validate'); checked = params.workflow; return { valid: true, problems: [], schemas: {} }; }, subscribe: () => () => {} } as any);
    const state = useEditor.getState();
    const baseRevision = workflowRevision(state);
    const patch = JSON.stringify({ operations: [
      { op: 'add', ref: 'sales_input', tool_id: 'core.input_data', config: { path: 'sales.csv' }, position: { x: 0, y: 0 } },
      { op: 'add', ref: 'sales_filter', tool_id: 'core.filter', config: { expression: '[Sales] > 100' }, position: { x: 220, y: 0 } },
      { op: 'connect', source_ref: 'sales_input', source_anchor: 'output', target_ref: 'sales_filter', target_anchor: 'input' },
    ] });
    const proposal = await prepareWorkflowProposal(state, patch, baseRevision);
    expect(proposal.validation.valid).toBe(true);
    expect(proposal.changes).toHaveLength(3);
    expect(checked.nodes).toHaveLength(2);
    expect(useEditor.getState().nodes).toHaveLength(0);
    useEditor.getState().commitGraphTransaction(proposal.candidate);
    expect(useEditor.getState().nodes).toHaveLength(2);
    useEditor.getState().undo();
    expect(useEditor.getState().nodes).toHaveLength(0);
  });

  it('rejects a stale workflow revision before candidate validation', async () => {
    useEditor.getState().loadGraph(emptyGraph as any);
    const state = useEditor.getState();
    await expect(prepareWorkflowProposal(state, '{"operations":[]}', 'rev_stale')).rejects.toThrow('workflow changed');
  });

  it('includes container membership and layout in the AI patch revision', () => {
    useEditor.getState().loadGraph({ ...emptyGraph, nodes: [
      { id: 'container', type: 'container', position: { x: 10, y: 20 }, data: { kind: 'container', caption: 'Group' } },
      { id: 'child', type: 'tool', position: { x: 15, y: 30 }, data: { kind: 'tool', tool: 'core.filter', config: {} } },
    ] } as any);
    const before = workflowRevision(useEditor.getState());
    useEditor.setState({ nodes: useEditor.getState().nodes.map((node) => node.id === 'child' ? { ...node, parentId: 'container' } : node) });
    expect(workflowRevision(useEditor.getState())).not.toBe(before);
  });

  it('lets the agent propose a question, Condition routes, and an Action target as one validated edit', async () => {
    useEditor.getState().loadGraph(emptyGraph as any);
    useEditor.getState().setEngineMeta({ tools: {
      'core.filter': { ...manifest('core.filter', ['input'], ['true', 'false']),
        config_schema: { type: 'object', properties: { expression: { type: 'string' } } } } as any,
    } });
    let checked: any;
    setTransport({ request: async (_method: string, params: any) => { checked = params.workflow; return { valid: true, problems: [], schemas: {} }; }, subscribe: () => () => {} } as any);
    const state = useEditor.getState(), baseRevision = workflowRevision(state);
    const patch = JSON.stringify({ operations: [
      { op: 'add', ref: 'filter', tool_id: 'core.filter', config: { expression: 'True' }, position: { x: 400, y: 100 } },
      { op: 'add_question', ref: 'expression', question: { id: 'sales_expression', prompt: 'Sales predicate', type: 'text', default: '[Sales] > 100' }, config_path: ['expression'], position: { x: 0, y: 100 } },
      { op: 'connect_action', source_ref: 'expression', target_ref: 'filter' },
      { op: 'add_question', ref: 'enabled', question: { id: 'enabled', prompt: 'Apply filter?', type: 'boolean', default: true }, config_path: ['expression'], position: { x: 0, y: 280 } },
      { op: 'add_condition', ref: 'condition', when: { question: 'enabled', operator: 'truthy' }, position: { x: 220, y: 280 } },
      { op: 'connect_condition', source_ref: 'condition', source_anchor: 'True Condition', target_ref: 'expression' },
    ] });
    const proposal = await prepareWorkflowProposal(state, patch, baseRevision);
    expect(proposal.validation.valid).toBe(true);
    expect(proposal.changes).toHaveLength(6);
    expect(checked.metadata.application.actions[0].when).toEqual({ question: 'enabled', operator: 'truthy' });
    expect(proposal.candidate.nodes.filter((n) => n.data.kind.startsWith('application-'))).toHaveLength(3);
    expect(useEditor.getState().nodes).toHaveLength(0);
  });

  it('lets the agent add and conditionally route a native Error Message', async () => {
    useEditor.getState().loadGraph(emptyGraph as any);
    let checked: any;
    setTransport({ request: async (_method: string, params: any) => { checked = params.workflow; return { valid: true, problems: [], schemas: {} }; }, subscribe: () => () => {} } as any);
    const state = useEditor.getState(), baseRevision = workflowRevision(state);
    const proposal = await prepareWorkflowProposal(state, JSON.stringify({ operations: [
      { op: 'add_question', ref: 'approved', question: { id: 'approved', prompt: 'Approved?', type: 'boolean', default: false }, config_path: ['value'], position: { x: 0, y: 0 } },
      { op: 'add_condition', ref: 'gate', when: { question: 'approved', operator: 'falsy' }, position: { x: 180, y: 0 } },
      { op: 'add_error_message', ref: 'invalid', message: 'Approval is required.', position: { x: 360, y: 0 } },
      { op: 'connect_condition', source_ref: 'gate', source_anchor: 'True Condition', target_ref: 'invalid' },
    ] }), baseRevision);
    expect(proposal.validation.valid).toBe(true);
    expect(checked.metadata.application.errors).toEqual([{ id: expect.any(String), message: 'Approval is required.', when: { question: 'approved', operator: 'falsy' } }]);
    expect(proposal.candidate.edges).toHaveLength(1);
    expect(useEditor.getState().nodes).toHaveLength(0);
  });

  it('lets the agent place and edit a workflow Comment through the editor graph', async () => {
    useEditor.getState().loadGraph(emptyGraph as any);
    setTransport({ request: async () => ({ valid: true, problems: [], schemas: {} }), subscribe: () => () => {} } as any);
    const state = useEditor.getState(), baseRevision = workflowRevision(state);
    const proposal = await prepareWorkflowProposal(state, JSON.stringify({ operations: [
      { op: 'add_comment', ref: 'note', text: 'Initial note', position: { x: 20, y: 30 } },
      { op: 'configure_comment', node_ref: 'note', text: 'Monthly sales summary' },
    ] }), baseRevision);
    expect(proposal.validation.valid).toBe(true);
    expect(proposal.candidate.nodes[0].data).toEqual({ kind: 'comment', text: 'Monthly sales summary' });
    expect(toDocument(proposal.candidate).metadata?.ui.comments[0].text).toBe('Monthly sales summary');
  });

  it('lets the agent group nodes in a native Tool Container, configure it, and ungroup atomically', async () => {
    const graph = { ...emptyGraph, nodes: [
      { id: 'input-node', type: 'tool', position: { x: 100, y: 120 }, data: { kind: 'tool', tool: 'core.input_data', config: {} } },
      { id: 'filter-node', type: 'tool', position: { x: 360, y: 120 }, data: { kind: 'tool', tool: 'core.filter', config: {} } },
    ] };
    useEditor.getState().loadGraph(graph as any);
    useEditor.getState().setEngineMeta({ tools: {
      'core.input_data': manifest('core.input_data', [], ['output']) as any,
      'core.filter': manifest('core.filter', ['input'], ['true', 'false']) as any,
    } });
    let checked: any;
    setTransport({ request: async (_method: string, params: any) => { checked = params.workflow; return { valid: true, problems: [], schemas: {} }; }, subscribe: () => () => {} } as any);
    const state = useEditor.getState(), baseRevision = workflowRevision(state);
    const proposal = await prepareWorkflowProposal(state, JSON.stringify({ operations: [
      { op: 'group', ref: 'sales_container', node_refs: ['node_1', 'node_2'], caption: 'Sales preparation' },
      { op: 'configure_container', node_ref: 'sales_container', caption: 'Sales preparation', disabled: true, collapsed: true },
    ] }), baseRevision);
    expect(proposal.validation.valid).toBe(true);
    const container = proposal.candidate.nodes.find((node) => node.data.kind === 'container');
    expect(container?.data).toEqual({ kind: 'container', caption: 'Sales preparation', disabled: true, collapsed: true });
    expect(proposal.candidate.nodes.filter((node) => node.parentId === container?.id)).toHaveLength(2);
    expect(proposal.consequences).toEqual(expect.arrayContaining([expect.stringContaining('Disable Tool Container')]));
    expect(checked.metadata.ui.containers[0].children).toHaveLength(2);
    expect(useEditor.getState().nodes).toHaveLength(2);

    useEditor.getState().loadGraph(proposal.candidate);
    const ungrouped = await prepareWorkflowProposal(useEditor.getState(), JSON.stringify({ operations: [
      { op: 'ungroup_container', node_ref: 'node_3' },
    ] }), workflowRevision(useEditor.getState()));
    expect(ungrouped.candidate.nodes.every((node) => !node.parentId)).toBe(true);
  });

  it('rejects Tool Container grouping for nodes already nested inside a container', async () => {
    useEditor.getState().loadGraph({ ...emptyGraph, nodes: [
      { id: 'outer', type: 'container', position: { x: 0, y: 0 }, data: { kind: 'container', caption: 'Outer' } },
      { id: 'child', type: 'tool', parentId: 'outer', position: { x: 40, y: 60 }, data: { kind: 'tool', tool: 'core.filter', config: {} } },
    ] } as any);
    setTransport({ request: async () => ({ valid: true, problems: [], schemas: {} }), subscribe: () => () => {} } as any);
    const state = useEditor.getState();
    await expect(prepareWorkflowProposal(state, JSON.stringify({ operations: [
      { op: 'group', ref: 'nested', node_refs: ['node_2'], caption: 'Nested' },
    ] }), workflowRevision(state))).rejects.toThrow('top-level nodes only');
  });

  it('preserves child coordinates and parent when ungrouping an inner container', async () => {
    const graph = { ...emptyGraph, nodes: [
      { id: 'outer', type: 'container', position: { x: 100, y: 80 }, data: { kind: 'container', caption: 'Outer' } },
      { id: 'inner', type: 'container', parentId: 'outer', position: { x: 20, y: 30 }, data: { kind: 'container', caption: 'Inner' } },
      { id: 'child', type: 'comment', parentId: 'inner', position: { x: 15, y: 25 }, data: { kind: 'comment', text: 'Note' } },
    ] };
    useEditor.getState().loadGraph(graph as any);
    setTransport({ request: async () => ({ valid: true, problems: [], schemas: {} }), subscribe: () => () => {} } as any);
    const state = useEditor.getState();
    const proposal = await prepareWorkflowProposal(state, JSON.stringify({ operations: [
      { op: 'ungroup_container', node_ref: 'node_2' },
    ] }), workflowRevision(state));
    const child = proposal.candidate.nodes.find((node) => node.id === 'child');
    expect(child?.parentId).toBe('outer');
    expect(child?.position).toEqual({ x: 35, y: 55 });
  });

  it('lets the agent propose a batch Control Parameter and its child Action target', async () => {
    useEditor.getState().loadGraph(emptyGraph as any);
    useEditor.getState().setEngineMeta({ tools: {
      'core.filter': { ...manifest('core.filter', ['input'], ['true', 'false']),
        config_schema: { type: 'object', properties: { field: { type: 'string' }, operator: { type: 'string' }, value: { type: 'number' } } } } as any,
    } });
    let checked: any;
    setTransport({ request: async (_method: string, params: any) => { checked = params.workflow; return { valid: true, problems: [], schemas: {} }; }, subscribe: () => () => {} } as any);
    const state = useEditor.getState(), baseRevision = workflowRevision(state);
    const proposal = await prepareWorkflowProposal(state, JSON.stringify({ operations: [
      { op: 'add', ref: 'filter', tool_id: 'core.filter', config: { field: 'Sales', operator: '>', value: 0 }, position: { x: 320, y: 0 } },
      { op: 'add_question', ref: 'threshold', question: { id: 'threshold', prompt: 'Minimum sales', type: 'control-parameter', control_field: 'minimum_sales' }, config_path: ['value'], position: { x: 0, y: 0 } },
      { op: 'connect_action', source_ref: 'threshold', target_ref: 'filter' },
    ] }), baseRevision);
    expect(proposal.validation.valid).toBe(true);
    expect(checked.metadata.application.questions).toContainEqual({ id: 'threshold', label: 'Minimum sales', type: 'control-parameter', control_field: 'minimum_sales' });
    expect(checked.metadata.application.actions).toContainEqual({ question: 'threshold', node_id: expect.any(String), config_path: ['value'] });
  });
});
