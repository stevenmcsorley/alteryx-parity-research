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
  metadata: { application: { questions: [{ id: 'q', type: 'text', default: 'x' }] }, ui: { containers: [{ id: 'c1', caption: 'Box', disabled: false, position: { x: 50, y: 50 }, size: { width: 600, height: 300 }, children: ['a', 'b'] }],
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
