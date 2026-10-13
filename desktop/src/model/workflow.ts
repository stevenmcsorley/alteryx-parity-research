// Canvas model <-> flowengine workflow document.
//
// Tool nodes map 1:1 to document nodes. Containers and comments are editor-only
// and are stored under metadata.ui so the engine ignores them and documents stay
// schema-valid. Child positions are relative to their container on the canvas
// and absolute in the document.
import type { Edge, Node } from '@xyflow/react';
import type { WorkflowDoc } from '../engine/types';

export type ToolNodeData = {
  kind: 'tool';
  tool: string;
  config: Record<string, unknown>;
  label?: string;
  annotation?: string;
  disabled?: boolean;
};
export type CommentNodeData = { kind: 'comment'; text: string };
export type ContainerNodeData = { kind: 'container'; caption: string; disabled?: boolean; collapsed?: boolean };
export type CanvasNodeData = ToolNodeData | CommentNodeData | ContainerNodeData;
export type CanvasNode = Node<CanvasNodeData>;
export type CanvasEdge = Edge;

export type CanvasGraph = { nodes: CanvasNode[]; edges: CanvasEdge[]; name: string; description?: string; profile?: string };

export function edgeId(source: string, sourceHandle: string, target: string, targetHandle: string): string {
  return `${source}:${sourceHandle}->${target}:${targetHandle}`;
}

function absolutePosition(node: CanvasNode, byId: Map<string, CanvasNode>): { x: number; y: number } {
  let { x, y } = node.position;
  let parent = node.parentId ? byId.get(node.parentId) : undefined;
  while (parent) {
    x += parent.position.x;
    y += parent.position.y;
    parent = parent.parentId ? byId.get(parent.parentId) : undefined;
  }
  return { x: Math.round(x), y: Math.round(y) };
}

function isDisabled(node: CanvasNode, byId: Map<string, CanvasNode>): boolean {
  if (node.data.kind === 'tool' && node.data.disabled) return true;
  let parent = node.parentId ? byId.get(node.parentId) : undefined;
  while (parent) {
    if (parent.data.kind === 'container' && parent.data.disabled) return true;
    parent = parent.parentId ? byId.get(parent.parentId) : undefined;
  }
  return false;
}

export function toDocument(g: CanvasGraph): WorkflowDoc {
  const byId = new Map(g.nodes.map((n) => [n.id, n]));
  const nodes: WorkflowDoc['nodes'] = [];
  const containers: any[] = [];
  const comments: any[] = [];
  for (const n of g.nodes) {
    const pos = absolutePosition(n, byId);
    if (n.data.kind === 'tool') {
      const d = n.data;
      const node: WorkflowDoc['nodes'][number] = { id: n.id, tool: d.tool, config: d.config, position: pos };
      if (d.label) node.label = d.label;
      if (d.annotation) node.annotation = d.annotation;
      if (isDisabled(n, byId)) node.disabled = true;
      nodes.push(node);
    } else if (n.data.kind === 'container') {
      containers.push({
        id: n.id, caption: n.data.caption, disabled: !!n.data.disabled, collapsed: !!n.data.collapsed, position: pos,
        size: { width: Number(n.style?.width ?? n.width ?? 320), height: Number(n.style?.height ?? n.height ?? 200) },
        parent: n.parentId ?? null,
        children: g.nodes.filter((c) => c.parentId === n.id).map((c) => c.id),
      });
    } else {
      comments.push({ id: n.id, text: n.data.text, position: pos, parent: n.parentId ?? null,
        size: { width: Number(n.style?.width ?? n.width ?? 200), height: Number(n.style?.height ?? n.height ?? 80) } });
    }
  }
  // tools explicitly disabled by their own flag keep it even if the container is re-enabled
  const edges = g.edges.map((e) => ({
    from: { node: e.source, anchor: e.sourceHandle ?? 'output' },
    to: { node: e.target, anchor: e.targetHandle ?? 'input' },
  }));
  const doc: WorkflowDoc = { format: 'flowengine.workflow', formatVersion: 1, name: g.name, nodes, edges };
  if (g.description) doc.description = g.description;
  const ownDisabled = Object.fromEntries(
    g.nodes.filter((n) => n.data.kind === 'tool' && n.data.disabled).map((n) => [n.id, true]),
  );
  doc.metadata = { ui: { containers, comments, ownDisabled } };
  if (g.profile) doc.metadata.profile = g.profile;
  return doc;
}

export function fromDocument(doc: WorkflowDoc): CanvasGraph {
  const ui = doc.metadata?.ui ?? {};
  const containers: any[] = ui.containers ?? [];
  const comments: any[] = ui.comments ?? [];
  const ownDisabled: Record<string, boolean> = ui.ownDisabled ?? {};
  const parentOf = new Map<string, string>();
  for (const c of containers) for (const child of c.children ?? []) parentOf.set(child, c.id);
  const absById = new Map<string, { x: number; y: number }>();
  for (const c of containers) absById.set(c.id, c.position);
  for (const n of doc.nodes) absById.set(n.id, n.position ?? { x: 0, y: 0 });
  for (const c of comments) absById.set(c.id, c.position);
  const rel = (id: string) => {
    const p = absById.get(id) ?? { x: 0, y: 0 };
    const parent = parentOf.get(id);
    if (!parent) return { ...p };
    const pp = absAll(parent);
    return { x: p.x - pp.x, y: p.y - pp.y };
  };
  const absAll = (id: string): { x: number; y: number } => absById.get(id) ?? { x: 0, y: 0 };

  // parents must precede children for React Flow
  const ordered: any[] = [];
  const seen = new Set<string>();
  const visit = (c: any) => {
    if (seen.has(c.id)) return;
    if (c.parent) {
      const p = containers.find((x) => x.id === c.parent);
      if (p) visit(p);
    }
    seen.add(c.id);
    ordered.push(c);
  };
  containers.forEach(visit);

  const nodes: CanvasNode[] = [
    ...ordered.map((c): CanvasNode => ({
      id: c.id, type: 'container', position: rel(c.id), parentId: parentOf.get(c.id),
      data: { kind: 'container', caption: c.caption, disabled: !!c.disabled, collapsed: !!c.collapsed },
      style: { width: c.size?.width ?? 320, height: c.size?.height ?? 200 },
    })),
    ...doc.nodes.map((n): CanvasNode => ({
      id: n.id, type: 'tool', position: rel(n.id), parentId: parentOf.get(n.id),
      data: { kind: 'tool', tool: n.tool, config: n.config ?? {}, label: n.label, annotation: n.annotation,
        disabled: ownDisabled[n.id] ?? (n.disabled && !parentOf.get(n.id) ? true : undefined) },
    })),
    ...comments.map((c): CanvasNode => ({
      id: c.id, type: 'comment', position: rel(c.id), parentId: parentOf.get(c.id),
      data: { kind: 'comment', text: c.text }, style: { width: c.size?.width ?? 200, height: c.size?.height ?? 80 },
    })),
  ];
  for (const n of nodes) if (!n.parentId) delete n.parentId;
  autoLayout(nodes, doc);
  const edges: CanvasEdge[] = doc.edges.map((e) => ({
    id: edgeId(e.from.node, e.from.anchor, e.to.node, e.to.anchor),
    source: e.from.node, sourceHandle: e.from.anchor, target: e.to.node, targetHandle: e.to.anchor,
  }));
  return { nodes, edges, name: doc.name ?? 'Untitled', description: doc.description, profile: doc.metadata?.profile };
}

/** Place tool nodes that have no stored position in columns by dependency depth. */
export function autoLayout(nodes: CanvasNode[], doc: WorkflowDoc): void {
  const missing = new Set(doc.nodes.filter((n) => !n.position).map((n) => n.id));
  if (!missing.size) return;
  const depth = new Map<string, number>();
  const ups = new Map<string, string[]>();
  for (const e of doc.edges) ups.set(e.to.node, [...(ups.get(e.to.node) ?? []), e.from.node]);
  const d = (id: string, seen = new Set<string>()): number => {
    if (depth.has(id)) return depth.get(id)!;
    if (seen.has(id)) return 0;
    seen.add(id);
    const v = Math.max(-1, ...(ups.get(id) ?? []).map((u) => d(u, seen))) + 1;
    depth.set(id, v);
    return v;
  };
  const perColumn = new Map<number, number>();
  for (const n of doc.nodes) {
    if (!missing.has(n.id)) continue;
    const col = d(n.id);
    const row = perColumn.get(col) ?? 0;
    perColumn.set(col, row + 1);
    const cn = nodes.find((x) => x.id === n.id);
    if (cn) cn.position = { x: 40 + col * 260, y: 40 + row * 130 };
  }
}

/** Next free id with a readable prefix derived from the tool id (e.g. "filter_3"). */
export function nextNodeId(prefix: string, existing: Iterable<string>): string {
  const base = prefix.replace(/^core\./, '').replace(/[^A-Za-z0-9_]/g, '_').slice(0, 40) || 'node';
  const taken = new Set(existing);
  let i = 1;
  while (taken.has(`${base}_${i}`)) i++;
  return `${base}_${i}`;
}

/** Default configuration from a JSON Schema: required properties get their defaults or empty values. */
export function defaultConfig(schema: any): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  const props = schema?.properties ?? {};
  for (const key of schema?.required ?? []) {
    const p = props[key] ?? {};
    if (p.default !== undefined) out[key] = structuredClone(p.default);
    else if (p.enum) out[key] = p.enum[0];
    else if (p.type === 'array') out[key] = [];
    else if (p.type === 'object') out[key] = {};
    else if (p.type === 'boolean') out[key] = false;
    else if (p.type === 'integer' || p.type === 'number') out[key] = p.minimum ?? 0;
    else out[key] = '';
  }
  return out;
}
