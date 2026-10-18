import { describe, expect, it } from 'vitest';
import { defaultConfig, fromDocument, nextNodeId, toDocument, type CanvasGraph } from './workflow';
import type { WorkflowDoc } from '../engine/types';

const doc: WorkflowDoc = {
  format: 'flowengine.workflow', formatVersion: 1, name: 'demo',
  nodes: [
    { id: 'a', tool: 'core.text_input', config: { fields: [], rows: [] }, position: { x: 100, y: 120 } },
    { id: 'b', tool: 'core.filter', config: { expression: '1' }, position: { x: 400, y: 120 }, label: 'Keep', disabled: true },
  ],
  edges: [{ from: { node: 'a', anchor: 'output' }, to: { node: 'b', anchor: 'input' } }],
  metadata: { application: { questions: [{ id: 'q', type: 'text', default: 'x' }] }, ui: { containers: [{ id: 'c1', caption: 'Box', disabled: false, collapsed: true, position: { x: 50, y: 50 }, size: { width: 600, height: 300 }, children: ['a', 'b'] }],
    comments: [{ id: 'n1', text: 'hello', position: { x: 10, y: 400 }, size: { width: 200, height: 80 } }], ownDisabled: { b: true } } },
};

describe('workflow model', () => {
  it('round-trips documents including containers, comments and absolute positions', () => {
    const g = fromDocument(doc);
    expect(g.nodes[0].id).toBe('c1'); // parents first
    const a = g.nodes.find((n) => n.id === 'a')!;
    expect(a.parentId).toBe('c1');
    expect(a.position).toEqual({ x: 50, y: 70 }); // relative to container
    const back = toDocument(g);
    expect(back.nodes.map((n) => [n.id, n.position])).toEqual([['a', { x: 100, y: 120 }], ['b', { x: 400, y: 120 }]]);
    expect(back.nodes[1].disabled).toBe(true);
    expect(back.edges).toEqual(doc.edges);
    expect(back.metadata!.ui.containers[0].children).toEqual(['a', 'b']);
    expect(back.metadata!.ui.containers[0].collapsed).toBe(true);
    expect(back.metadata!.ui.comments[0].text).toBe('hello');
    expect(back.metadata!.application).toEqual(doc.metadata!.application);
  });

  it('disabling a container disables contained tools in the document only', () => {
    const g = fromDocument(doc);
    const c = g.nodes.find((n) => n.id === 'c1')!;
    (c.data as any).disabled = true;
    const out = toDocument(g as CanvasGraph);
    expect(out.nodes.every((n) => n.disabled)).toBe(true);
    expect(out.metadata!.ui.ownDisabled).toEqual({ b: true });
  });

  it('renders application actions as configuration edges and preserves the workflow data DAG', () => {
    const appDoc = { ...doc, metadata: { ...doc.metadata!, application: {
      questions: [{ id: 'threshold', label: 'Threshold', type: 'text', default: '[amount] > 2' }],
      actions: [{ question: 'threshold', node_id: 'b', config_path: ['expression'] }],
    } } } as WorkflowDoc;
    const graph = fromDocument(appDoc);
    expect(graph.nodes.some((n) => n.data.kind === 'application-action')).toBe(true);
    expect(graph.edges).toHaveLength(2);
    expect(graph.edges.find((e) => e.data?.kind === 'application-action')?.targetHandle).toBe('__action__');
    const saved = toDocument(graph);
    expect(saved.edges).toEqual(doc.edges);
    expect(saved.metadata?.application?.actions).toEqual(appDoc.metadata?.application?.actions);
    expect(saved.metadata?.application?.questions[0]).toEqual({ id: 'threshold', label: 'Threshold', type: 'text', default: '[amount] > 2' });
  });

  it('round-trips Explorer Boxes, sourced options and formula / input actions', () => {
    const appDoc = { ...doc, metadata: { ...doc.metadata!, ui: { ...doc.metadata!.ui, explorers: [{ id: 'x1', url: 'docs', hide_address_bar: true, position: { x: 5, y: 600 }, size: { width: 400, height: 300 } }] },
      application: {
        questions: [{ id: 'region', label: 'Region', type: 'choice', source: { kind: 'file', path: 'regions.csv', field: 'region' } },
          { id: 'month', label: 'Month', type: 'file' }],
        actions: [{ question: 'region', node_id: 'b', config_path: ['expression'], kind: 'formula', expression: '"[region] = \'" + [region] + "\'"' },
          { question: 'month', node_id: 'a', config_path: ['path'], kind: 'input' }],
      } } } as WorkflowDoc;
    const graph = fromDocument(appDoc);
    expect(graph.nodes.find((n) => n.id === 'x1')?.data).toEqual({ kind: 'explorer', url: 'docs', hide_address_bar: true });
    const saved = toDocument(graph);
    expect(saved.metadata!.ui.explorers[0]).toMatchObject({ id: 'x1', url: 'docs', hide_address_bar: true, position: { x: 5, y: 600 }, size: { width: 400, height: 300 } });
    expect(saved.metadata?.application?.actions).toEqual(appDoc.metadata?.application?.actions);
    expect(saved.metadata?.application?.questions[0].source).toEqual({ kind: 'file', path: 'regions.csv', field: 'region' });
  });

  it('preserves conditional Action predicates across desktop workflow roundtrips', () => {
    const appDoc = { ...doc, metadata: { ...doc.metadata!, application: {
      questions: [{ id: 'enabled', label: 'Enable', type: 'boolean', default: false },
        { id: 'replacement', label: 'Expression', type: 'text', default: '[amount] > 2' }],
      actions: [{ question: 'replacement', node_id: 'b', config_path: ['expression'],
        when: { question: 'enabled', operator: 'equals', value: true } }],
    } } } as WorkflowDoc;
    const graph = fromDocument(appDoc);
    const action = graph.nodes.find((n) => n.data.kind === 'application-action')!;
    expect((action.data as any).when).toEqual({ question: 'enabled', operator: 'equals', value: true });
    const roundtripped = toDocument(graph);
    expect(roundtripped.metadata?.application?.actions[0].when).toEqual({ question: 'enabled', operator: 'equals', value: true });
  });

  it('round-trips Condition nodes and translates both branches into executable action predicates', () => {
    const graph: CanvasGraph = {
      name: 'condition routes', nodes: [
        { id: 'q', type: 'applicationAction', position: { x: 0, y: 0 }, data: { kind: 'application-action',
          question: { id: 'enabled', prompt: 'Enabled?', type: 'boolean', default: false }, config_path: ['expression'] } },
        { id: 'condition', type: 'applicationCondition', position: { x: 220, y: 0 }, data: { kind: 'application-condition',
          when: { question: 'enabled', operator: 'equals', value: true } } },
        { id: 'yes', type: 'applicationAction', position: { x: 440, y: 0 }, data: { kind: 'application-action',
          question: { id: 'yes-expression', prompt: 'Yes expression', type: 'text', default: '1' }, config_path: ['expression'] } },
        { id: 'no', type: 'applicationAction', position: { x: 440, y: 140 }, data: { kind: 'application-action',
          question: { id: 'no-expression', prompt: 'No expression', type: 'text', default: '0' }, config_path: ['expression'] } },
        { id: 'filter-yes', type: 'tool', position: { x: 700, y: 0 }, data: { kind: 'tool', tool: 'core.filter', config: { expression: '1' } } },
        { id: 'filter-no', type: 'tool', position: { x: 700, y: 140 }, data: { kind: 'tool', tool: 'core.filter', config: { expression: '1' } } },
      ], edges: [
        { id: 'condition:True Condition->yes:Condition', source: 'condition', sourceHandle: 'True Condition', target: 'yes', targetHandle: 'Condition', data: { kind: 'application-condition' } },
        { id: 'condition:False Condition->no:Condition', source: 'condition', sourceHandle: 'False Condition', target: 'no', targetHandle: 'Condition', data: { kind: 'application-condition' } },
        { id: 'yes:Action->filter-yes:__action__', source: 'yes', sourceHandle: 'Action', target: 'filter-yes', targetHandle: '__action__', data: { kind: 'application-action' } },
        { id: 'no:Action->filter-no:__action__', source: 'no', sourceHandle: 'Action', target: 'filter-no', targetHandle: '__action__', data: { kind: 'application-action' } },
      ],
    };
    const saved = toDocument(graph);
    expect(saved.metadata?.application?.actions.map((a: any) => a.when)).toEqual([
      { question: 'enabled', operator: 'equals', value: true },
      { question: 'enabled', operator: 'not_equals', value: true },
    ]);
    const loaded = fromDocument(saved);
    expect(loaded.nodes.some((n) => n.data.kind === 'application-condition')).toBe(true);
    expect(loaded.edges.filter((e) => e.data?.kind === 'application-condition')).toHaveLength(2);
    expect(toDocument(loaded).metadata?.application?.actions.map((a: any) => a.when)).toEqual(saved.metadata?.application?.actions.map((a: any) => a.when));
  });

  it('round-trips a conditional native Error Message node without adding a row-data edge', () => {
    const graph: CanvasGraph = { name: 'application error', nodes: [
      { id: 'condition', type: 'applicationCondition', position: { x: 0, y: 0 }, data: { kind: 'application-condition',
        when: { question: 'valid', operator: 'truthy' } } },
      { id: 'error', type: 'applicationError', position: { x: 200, y: 0 }, data: { kind: 'application-error', message: 'Provide a valid value.' } },
    ], edges: [{ id: 'condition:True Condition->error:Condition', source: 'condition', sourceHandle: 'True Condition',
      target: 'error', targetHandle: 'Condition', data: { kind: 'application-condition' } }] };
    const saved = toDocument(graph);
    expect(saved.edges).toEqual([]);
    expect(saved.metadata?.application?.errors).toEqual([{ id: 'error', message: 'Provide a valid value.',
      when: { question: 'valid', operator: 'truthy' } }]);
    const loaded = fromDocument(saved);
    expect(loaded.nodes.find((n) => n.id === 'error')?.data).toEqual({ kind: 'application-error', message: 'Provide a valid value.', when: { question: 'valid', operator: 'truthy' } });
    expect(loaded.edges).toHaveLength(1);
    expect(toDocument(loaded).metadata?.application?.errors).toEqual(saved.metadata?.application?.errors);
  });

  it('auto-lays out documents without positions by dependency depth', () => {
    const g = fromDocument({ ...doc, metadata: undefined, nodes: doc.nodes.map(({ position: _p, ...n }) => n) });
    const a = g.nodes.find((n) => n.id === 'a')!, b = g.nodes.find((n) => n.id === 'b')!;
    expect(b.position.x).toBeGreaterThan(a.position.x);
  });

  it('allocates readable unique ids and default configs', () => {
    expect(nextNodeId('core.filter', ['filter_1', 'filter_2'])).toBe('filter_3');
    expect(defaultConfig({ type: 'object', required: ['n', 'mode', 'keys'], properties: {
      n: { type: 'integer', minimum: 0 }, mode: { enum: ['x', 'y'] }, keys: { type: 'array' }, opt: { type: 'string', default: 'z' } } }))
      .toEqual({ n: 0, mode: 'x', keys: [] });
  });
});
