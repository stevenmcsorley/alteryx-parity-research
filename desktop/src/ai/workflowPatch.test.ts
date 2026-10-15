import { afterEach, describe, expect, it } from 'vitest';
import { prepareWorkflowProposal, workflowRevision } from './workflowPatch';
import { useEditor } from '../state/store';
import { setTransport } from '../state/engineActions';

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
});
