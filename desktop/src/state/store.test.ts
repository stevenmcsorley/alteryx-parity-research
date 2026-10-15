import { beforeEach, describe, expect, it } from 'vitest';
import { useEditor } from './store';
import { statusOf } from '../panels/Palette';
import { previewToCsv } from './engineActions';
import { fromDocument, toDocument } from '../model/workflow';
import type { ToolManifest } from '../engine/types';

const manifest = (id: string, inputs: [string, boolean][], outputs: string[]): ToolManifest => ({
  tool_id: id, version: '1.0.0', display_name: id, catalog_ids: [], description: '', side_effects: 'none',
  deterministic: true, dependencies: [], config_schema: { type: 'object', properties: {} },
  inputs: inputs.map(([name, multiple]) => ({ name, label: name, required: true, multiple, description: '' })),
  outputs: outputs.map((name) => ({ name, label: name, required: true, multiple: false, description: '' })),
});

beforeEach(() => {
  useEditor.getState().newWorkflow();
  useEditor.setState({ tools: {
    'core.text_input': manifest('core.text_input', [], ['output']),
    'core.filter': manifest('core.filter', [['input', false]], ['true', 'false']),
    'core.union': manifest('core.union', [['inputs', true]], ['output']),
  } });
});

describe('editor store', () => {
  it('adds tools with unique ids and supports undo/redo', () => {
    const s = useEditor.getState();
    const a = s.addTool('core.text_input', { x: 0, y: 0 });
    const b = useEditor.getState().addTool('core.text_input', { x: 0, y: 0 });
    expect([a, b]).toEqual(['text_input_1', 'text_input_2']);
    useEditor.getState().undo();
    expect(useEditor.getState().nodes.map((n) => n.id)).toEqual(['text_input_1']);
    useEditor.getState().redo();
    expect(useEditor.getState().nodes).toHaveLength(2);
    expect(useEditor.getState().dirty).toBe(true);
  });

  it('preserves Date and List Box application question types in workflow metadata', () => {
    const date = useEditor.getState().addApplicationAction({ x: 0, y: 0 }, 'date', 'Start date');
    const list = useEditor.getState().addApplicationAction({ x: 0, y: 160 }, 'list-box', 'Regions');
    const graph = useEditor.getState();
    expect((graph.nodes.find((n) => n.id === date)!.data as any).question.type).toBe('date');
    expect((graph.nodes.find((n) => n.id === list)!.data as any).question).toMatchObject({ type: 'multi-choice', options: ['Option 1', 'Option 2'] });
    const saved = toDocument({ ...graph, description: undefined } as any);
    expect(saved.metadata?.application?.questions.map((q: any) => q.type)).toEqual(['date', 'multi-choice']);
    expect(new Set(saved.metadata?.application?.questions.map((q: any) => q.id)).size).toBe(2);
  });

  it('enforces anchor cardinality, self-loops and cycles', () => {
    const st = useEditor.getState();
    const t1 = st.addTool('core.text_input', { x: 0, y: 0 });
    const t2 = useEditor.getState().addTool('core.text_input', { x: 0, y: 0 });
    const f = useEditor.getState().addTool('core.filter', { x: 0, y: 0 });
    const u = useEditor.getState().addTool('core.union', { x: 0, y: 0 });
    const c = useEditor.getState().connect;
    expect(c({ source: t1, sourceHandle: 'output', target: f, targetHandle: 'input' })).toBeNull();
    expect(c({ source: t2, sourceHandle: 'output', target: f, targetHandle: 'input' })).toMatch(/accepts one connection/);
    expect(c({ source: t1, sourceHandle: 'output', target: u, targetHandle: 'inputs' })).toBeNull();
    expect(c({ source: t2, sourceHandle: 'output', target: u, targetHandle: 'inputs' })).toBeNull();
    expect(c({ source: f, sourceHandle: 'true', target: f, targetHandle: 'input' })).toMatch(/itself/);
    const f2 = useEditor.getState().addTool('core.filter', { x: 0, y: 0 });
    expect(c({ source: u, sourceHandle: 'output', target: f2, targetHandle: 'input' })).toBeNull();
    expect(useEditor.getState().edges).toHaveLength(4);
  });

  it('keeps Action target wiring out of row-data edges and round-trips app metadata', () => {
    const target = useEditor.getState().addTool('core.filter', { x: 400, y: 100 });
    const action = useEditor.getState().addApplicationAction({ x: 100, y: 100 });
    const source = useEditor.getState().addTool('core.text_input', { x: 0, y: 0 });
    expect(useEditor.getState().connect({ source, sourceHandle: 'output', target, targetHandle: '__action__' })).toMatch(/Unknown input anchor/);
    expect(useEditor.getState().connect({ source: action, sourceHandle: 'Action', target, targetHandle: 'input' })).toMatch(/Action connections/);
    expect(useEditor.getState().connect({ source: action, sourceHandle: 'Action', target, targetHandle: '__action__' })).toBeNull();
    const doc = toDocument({ ...useEditor.getState(), description: undefined } as any);
    expect(doc.edges).toEqual([]);
    expect(doc.metadata?.application?.questions).toEqual([{ id: 'question_1', label: 'Enter a value', type: 'text' }]);
    expect(doc.metadata?.application?.actions).toEqual([{ question: 'question_1', node_id: target, config_path: ['expression'] }]);
    const reopened = fromDocument(doc);
    expect(reopened.nodes.some((n) => n.data.kind === 'application-action')).toBe(true);
    expect(reopened.edges).toHaveLength(1);
    expect(reopened.edges[0].data?.kind).toBe('application-action');
    expect(toDocument(reopened).edges).toEqual([]);
  });

  it('connects Condition routes only to application control inputs', () => {
    const st = useEditor.getState();
    const questionAction = st.addApplicationAction({ x: 0, y: 0 });
    const questionTarget = useEditor.getState().addTool('core.filter', { x: 300, y: 0 });
    const yesAction = useEditor.getState().addApplicationAction({ x: 0, y: 200 });
    const yesTarget = useEditor.getState().addTool('core.filter', { x: 300, y: 200 });
    const noAction = useEditor.getState().addApplicationAction({ x: 0, y: 400 });
    const noTarget = useEditor.getState().addTool('core.filter', { x: 300, y: 400 });
    const condition = useEditor.getState().addApplicationCondition({ x: 150, y: 100 });
    const error = useEditor.getState().addApplicationError({ x: 450, y: 400 });
    const connect = useEditor.getState().connect;
    expect(connect({ source: condition, sourceHandle: 'True Condition', target: yesAction, targetHandle: 'Condition' })).toBeNull();
    expect(connect({ source: condition, sourceHandle: 'False Condition', target: noAction, targetHandle: 'Condition' })).toBeNull();
    expect(connect({ source: condition, sourceHandle: 'False Condition', target: error, targetHandle: 'Condition' })).toBeNull();
    expect(connect({ source: yesAction, sourceHandle: 'Action', target: yesTarget, targetHandle: '__action__' })).toBeNull();
    expect(connect({ source: noAction, sourceHandle: 'Action', target: noTarget, targetHandle: '__action__' })).toBeNull();
    expect(connect({ source: questionAction, sourceHandle: 'Action', target: questionTarget, targetHandle: '__action__' })).toBeNull();
    expect(connect({ source: condition, sourceHandle: 'output', target: yesAction, targetHandle: 'Condition' })).toMatch(/Condition outputs/);
    const doc = toDocument({ ...useEditor.getState(), description: undefined } as any);
    const questionId = (useEditor.getState().nodes.find((n) => n.id === questionAction)!.data as any).question.id;
    expect(doc.metadata?.application?.actions.find((a: any) => a.node_id === yesTarget)?.when.operator).toBe('truthy');
    expect(doc.metadata?.application?.actions.find((a: any) => a.node_id === noTarget)?.when.operator).toBe('falsy');
    expect(doc.metadata?.ui.applicationConditions).toHaveLength(1);
    expect(doc.metadata?.application?.errors).toEqual([{ id: error, message: 'Please correct the application inputs before running.',
      when: { question: questionId, operator: 'falsy' } }]);
  });

  it('groups, ungroups, copies and pastes', () => {
    const a = useEditor.getState().addTool('core.text_input', { x: 100, y: 100 });
    useEditor.getState().addTool('core.filter', { x: 400, y: 100 });
    useEditor.getState().selectAll();
    const cid = useEditor.getState().groupSelected()!;
    const inner = useEditor.getState().nodes.find((n) => n.id === a)!;
    expect(inner.parentId).toBe(cid);
    useEditor.getState().ungroup(cid);
    expect(useEditor.getState().nodes.find((n) => n.id === a)!.position).toEqual({ x: 100, y: 100 });
    useEditor.getState().selectAll();
    useEditor.getState().duplicateSelected();
    expect(useEditor.getState().nodes.map((n) => n.id).sort()).toEqual(['filter_1', 'filter_2', 'text_input_1', 'text_input_2']);
  });

  it('ignores run results that belong to another run', () => {
    useEditor.getState().runStarted('r1');
    useEditor.getState().runFinished({ run_id: 'other', status: 'succeeded', error: null, manifest: {}, events: [], previews: {}, browse: {} });
    expect(useEditor.getState().run.status).toBe('running');
    useEditor.getState().runFinished({ run_id: 'r1', status: 'succeeded', error: null, manifest: {}, previews: {}, browse: {},
      events: [{ seq: 0, run_id: 'r1', ts: '', type: 'node-completed', node_id: 'x', output_rows: { output: 3 }, duration_ms: 1 }] });
    expect(useEditor.getState().run.nodeRows).toEqual({ x: { output: 3 } });
  });
});

describe('palette status and export helpers', () => {
  const base = { id: 'x', name: 'X', category: 'Join', documentation_url: null, implementation_status: 'implemented' as const,
    verification_status: 'not-tested' as const, kernel: 'core.x', external_prerequisites_blocking: false, unavailable_reason: null };
  it('labels statuses honestly', () => {
    expect(statusOf(base).label).toBe('Ready');
    expect(statusOf({ ...base, implementation_status: 'in-progress' }).label).toBe('In progress');
    expect(statusOf({ ...base, verification_status: 'partial' }).label).toBe('Ref-tested');
    expect(statusOf({ ...base, kernel: null, unavailable_reason: 'no kernel' }).label).toBe('Unavailable');
    expect(statusOf({ ...base, kernel: null, external_prerequisites_blocking: true }).label).toBe('Blocked');
  });
  it('quotes CSV correctly', () => {
    expect(previewToCsv([{ name: 'a' }, { name: 'b,c' }], [['x"y', null], ['1', '2\n3']]))
      .toBe('a,"b,c"\n"x""y",\n1,"2\n3"\n');
  });
});
