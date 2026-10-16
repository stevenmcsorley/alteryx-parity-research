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
export type ApplicationActionNodeData = {
  kind: 'application-action';
  question: { id: string; prompt: string; label?: string; description?: string; type: 'text' | 'text-area' | 'number' | 'boolean' | 'choice' | 'multi-choice' | 'date' | 'file' | 'folder' | 'control-parameter' | 'tree'; control_field?: string; required?: boolean; default?: unknown; options?: unknown[]; secret?: boolean; minimum?: number; maximum?: number; mode?: 'open' | 'save'; extensions?: string[] };
  config_path: string[];
  when?: { question: string; operator: 'equals' | 'not_equals' | 'truthy' | 'falsy' | 'contains' | 'not_contains' | 'greater_than' | 'greater_than_or_equal' | 'less_than' | 'less_than_or_equal'; value?: unknown };
  source_action_index?: number;
};
export type ApplicationPredicate = NonNullable<ApplicationActionNodeData['when']>;
export type ApplicationConditionNodeData = { kind: 'application-condition'; when: ApplicationPredicate };
export type ApplicationErrorNodeData = { kind: 'application-error'; message: string; when?: ApplicationPredicate };
export type CanvasNodeData = ToolNodeData | CommentNodeData | ContainerNodeData | ApplicationActionNodeData | ApplicationConditionNodeData | ApplicationErrorNodeData;
export type CanvasNode = Node<CanvasNodeData>;
export type CanvasEdge = Edge<{ kind?: 'application-action' | 'application-condition' }>;

export type CanvasGraph = { nodes: CanvasNode[]; edges: CanvasEdge[]; name: string; description?: string; profile?: string; metadata?: Record<string, any> };

export function edgeId(source: string, sourceHandle: string, target: string, targetHandle: string): string {
  return `${source}:${sourceHandle}->${target}:${targetHandle}`;
}

function branchPredicate(when: ApplicationPredicate, invert: boolean): ApplicationPredicate {
  if (!invert) return structuredClone(when);
  const inverse: Record<ApplicationPredicate['operator'], ApplicationPredicate['operator']> = {
    equals: 'not_equals', not_equals: 'equals', truthy: 'falsy', falsy: 'truthy',
    contains: 'not_contains', not_contains: 'contains', greater_than: 'less_than_or_equal',
    greater_than_or_equal: 'less_than', less_than: 'greater_than_or_equal', less_than_or_equal: 'greater_than',
  };
  return { ...structuredClone(when), operator: inverse[when.operator] };
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
  const applicationActions: any[] = [];
  const applicationConditions: any[] = [];
  const applicationErrors: any[] = [];
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
    } else if (n.data.kind === 'application-action') {
      const link = g.edges.find((e) => e.source === n.id && e.sourceHandle === 'Action' && e.data?.kind === 'application-action');
      const conditionLink = g.edges.find((e) => e.target === n.id && e.data?.kind === 'application-condition');
      applicationActions.push({ id: n.id, question: structuredClone(n.data.question), config_path: [...n.data.config_path],
        target_node: link?.target ?? null, position: pos, source_action_index: n.data.source_action_index,
        when: n.data.when ? structuredClone(n.data.when) : undefined,
        condition_node: conditionLink?.source ?? null, condition_branch: conditionLink?.sourceHandle ?? null });
    } else if (n.data.kind === 'application-condition') {
      applicationConditions.push({ id: n.id, when: structuredClone(n.data.when), position: pos });
    } else if (n.data.kind === 'application-error') {
      const conditionLink = g.edges.find((e) => e.target === n.id && e.data?.kind === 'application-condition');
      const conditionNode = conditionLink && g.nodes.find((x) => x.id === conditionLink.source && x.data.kind === 'application-condition');
      const when = conditionNode?.data.kind === 'application-condition'
        ? branchPredicate(conditionNode.data.when, conditionLink?.sourceHandle === 'False Condition') : n.data.when;
      applicationErrors.push({ id: n.id, message: n.data.message, when, condition_node: conditionLink?.source ?? null,
        condition_branch: conditionLink?.sourceHandle ?? null, position: pos });
    } else {
      comments.push({ id: n.id, text: n.data.text, position: pos, parent: n.parentId ?? null,
        size: { width: Number(n.style?.width ?? n.width ?? 200), height: Number(n.style?.height ?? n.height ?? 80) } });
    }
  }
  // tools explicitly disabled by their own flag keep it even if the container is re-enabled
  const edges = g.edges.filter((e) => !['application-action', 'application-condition'].includes(e.data?.kind ?? '')).map((e) => ({
    from: { node: e.source, anchor: e.sourceHandle ?? 'output' },
    to: { node: e.target, anchor: e.targetHandle ?? 'input' },
  }));
  const doc: WorkflowDoc = { format: 'flowengine.workflow', formatVersion: 1, name: g.name, nodes, edges };
  if (g.description) doc.description = g.description;
  const ownDisabled = Object.fromEntries(
    g.nodes.filter((n) => n.data.kind === 'tool' && n.data.disabled).map((n) => [n.id, true]),
  );
  const metadata = structuredClone(g.metadata ?? {});
  const actionNodes = g.nodes.filter((n) => n.data.kind === 'application-action');
  if (actionNodes.length || applicationErrors.length || (metadata.ui?.applicationActions?.length ?? 0) > 0 || (metadata.application?.actions?.length ?? 0) > 0 || (metadata.application?.errors?.length ?? 0) > 0) {
    const old = metadata.application ?? { questions: [], actions: [] };
    const baselineActions: any[] = metadata.ui?.applicationActions ?? [];
    const currentQuestionIds = new Set(actionNodes.map((n) => (n.data as ApplicationActionNodeData).question.id));
    const removedQuestionIds = new Set(baselineActions.map((a) => a.question?.id).filter((id: unknown): id is string => typeof id === 'string' && !currentQuestionIds.has(id)));
    const questions = (old.questions ?? []).filter((q: any) => !removedQuestionIds.has(q.id));
    const replaced = new Set([...baselineActions.map((a) => a.source_action_index), ...actionNodes.map((n) => (n.data as ApplicationActionNodeData).source_action_index)]
      .filter((x): x is number => Number.isInteger(x)));
    const actions: any[] = (old.actions ?? []).filter((action: any, i: number) => !replaced.has(i) &&
      !baselineActions.some((base) => base.target_node === action.node_id && base.question?.id === action.question &&
        JSON.stringify(base.config_path) === JSON.stringify(action.config_path)));
    for (const n of actionNodes) {
      const d = n.data as ApplicationActionNodeData;
      const { prompt, label: _label, ...questionFields } = d.question;
      const q = { ...questionFields, label: prompt };
      const qi = questions.findIndex((x: any) => x.id === q.id);
      if (qi >= 0) questions[qi] = q; else questions.push(q);
      const linked = g.edges.find((e) => e.source === n.id && e.sourceHandle === 'Action' && e.data?.kind === 'application-action');
      if (!linked) continue;
      const target = byId.get(linked.target);
      if (target?.data.kind !== 'tool') continue;
      const conditionLink = g.edges.find((e) => e.target === n.id && e.data?.kind === 'application-condition');
      const conditionNode = conditionLink && g.nodes.find((x) => x.id === conditionLink.source && x.data.kind === 'application-condition');
      const conditionPredicate = conditionNode?.data.kind === 'application-condition'
        ? branchPredicate(conditionNode.data.when, conditionLink?.sourceHandle === 'False Condition') : undefined;
      const when = conditionPredicate ?? d.when;
      const action = { question: q.id, node_id: target.id, config_path: d.config_path,
        ...(when ? { when: structuredClone(when) } : {}) };
      const duplicate = actions.findIndex((oldAction: any) => oldAction.question === action.question &&
        oldAction.node_id === action.node_id && JSON.stringify(oldAction.config_path) === JSON.stringify(action.config_path) &&
        JSON.stringify(oldAction.when ?? null) === JSON.stringify(action.when ?? null));
      if (duplicate >= 0) actions.splice(duplicate, 1);
      actions.push(action);
    }
    const previousErrors: any[] = metadata.ui?.applicationErrors ?? [];
    const replacedErrors = new Set(previousErrors.map((error) => error.id));
    const errors = (old.errors ?? []).filter((error: any) => !replacedErrors.has(error.id));
    errors.push(...applicationErrors.map(({ condition_node: _node, condition_branch: _branch, position: _position, ...error }) => error));
    metadata.application = { ...old, questions, actions, errors };
  }
  doc.metadata = { ...metadata, ui: { containers, comments, ownDisabled, applicationActions, applicationConditions, applicationErrors } };
  if (g.profile) doc.metadata.profile = g.profile;
  return doc;
}

export function fromDocument(doc: WorkflowDoc): CanvasGraph {
  const ui = doc.metadata?.ui ?? {};
  const containers: any[] = ui.containers ?? [];
  const comments: any[] = ui.comments ?? [];
  const applicationActions: any[] = ui.applicationActions ?? [];
  const applicationConditions: any[] = ui.applicationConditions ?? [];
  const applicationErrors: any[] = ui.applicationErrors ?? (doc.metadata?.application?.errors ?? []).map((e: any) => e);
  const ownDisabled: Record<string, boolean> = ui.ownDisabled ?? {};
  const parentOf = new Map<string, string>();
  for (const c of containers) for (const child of c.children ?? []) parentOf.set(child, c.id);
  const absById = new Map<string, { x: number; y: number }>();
  for (const c of containers) absById.set(c.id, c.position);
  for (const n of doc.nodes) absById.set(n.id, n.position ?? { x: 0, y: 0 });
  for (const c of comments) absById.set(c.id, c.position);
  for (const a of applicationActions) absById.set(a.id, a.position ?? { x: 0, y: 0 });
  for (const c of applicationConditions) absById.set(c.id, c.position ?? { x: 0, y: 0 });
  for (const e of applicationErrors) absById.set(e.id, e.position ?? { x: 0, y: 0 });
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
    ...applicationActions.map((a: any): CanvasNode => ({
      id: a.id, type: 'applicationAction', position: rel(a.id),
      data: { kind: 'application-action', question: { ...a.question, prompt: a.question.prompt ?? a.question.label ?? a.question.id },
        config_path: a.config_path, when: a.when ? structuredClone(a.when) : undefined, source_action_index: a.source_action_index },
    })),
    ...applicationConditions.map((c: any): CanvasNode => ({
      id: c.id, type: 'applicationCondition', position: rel(c.id),
      data: { kind: 'application-condition', when: structuredClone(c.when) },
    })),
    ...applicationErrors.map((e: any): CanvasNode => ({
      id: e.id, type: 'applicationError', position: rel(e.id),
      data: { kind: 'application-error', message: e.message, when: e.when ? structuredClone(e.when) : undefined },
    })),
  ];
  for (const n of nodes) if (!n.parentId) delete n.parentId;
  autoLayout(nodes, doc);
  const edges: CanvasEdge[] = doc.edges.map((e) => ({
    id: edgeId(e.from.node, e.from.anchor, e.to.node, e.to.anchor),
    source: e.from.node, sourceHandle: e.from.anchor, target: e.to.node, targetHandle: e.to.anchor,
  }));
  for (const a of applicationActions) {
    const target = a.target_node;
    if (target && doc.nodes.some((n) => n.id === target)) edges.push({
      id: `${a.id}:Action->${target}:__action__`, source: a.id, sourceHandle: 'Action',
      target, targetHandle: '__action__', data: { kind: 'application-action' },
      style: { stroke: '#805ad5', strokeDasharray: '5 4' }, label: 'Action',
    });
    if (a.condition_node && ['True Condition', 'False Condition'].includes(a.condition_branch)) edges.push({
      id: `${a.condition_node}:${a.condition_branch}->${a.id}:Condition`, source: a.condition_node,
      sourceHandle: a.condition_branch, target: a.id, targetHandle: 'Condition', data: { kind: 'application-condition' },
      style: { stroke: '#dd8a36', strokeDasharray: '4 4' }, label: a.condition_branch === 'True Condition' ? 'True' : 'False',
    });
  }
  for (const e of applicationErrors) if (e.condition_node && ['True Condition', 'False Condition'].includes(e.condition_branch)) edges.push({
    id: `${e.condition_node}:${e.condition_branch}->${e.id}:Condition`, source: e.condition_node,
    sourceHandle: e.condition_branch, target: e.id, targetHandle: 'Condition', data: { kind: 'application-condition' },
    style: { stroke: '#dd8a36', strokeDasharray: '4 4' }, label: e.condition_branch === 'True Condition' ? 'True' : 'False',
  });
  if (!applicationActions.length) {
    const app = doc.metadata?.application ?? {};
    for (const [i, action] of ((app.actions ?? []) as any[]).entries()) {
      const question = (app.questions ?? []).find((q: any) => q.id === action.question);
      if (!question || !doc.nodes.some((n) => n.id === action.node_id)) continue;
      const id = `application_action_${i + 1}`;
      const position = { x: 180, y: 80 + i * 110 };
      nodes.push({ id, type: 'applicationAction', position,
        data: { kind: 'application-action', question: { ...structuredClone(question), prompt: question.prompt ?? question.label ?? question.id },
          config_path: [...action.config_path], when: action.when ? structuredClone(action.when) : undefined, source_action_index: i } });
      edges.push({ id: `${id}:Action->${action.node_id}:__action__`, source: id, sourceHandle: 'Action',
        target: action.node_id, targetHandle: '__action__', data: { kind: 'application-action' },
        style: { stroke: '#805ad5', strokeDasharray: '5 4' }, label: 'Action' });
      applicationActions.push({ id, question: structuredClone(question), config_path: [...action.config_path],
        target_node: action.node_id, position, source_action_index: i, when: action.when ? structuredClone(action.when) : undefined });
    }
  }
  const metadata = doc.metadata ? structuredClone(doc.metadata) : {};
  metadata.ui = { ...(metadata.ui ?? {}), applicationActions, applicationConditions, applicationErrors };
  return { nodes, edges, name: doc.name ?? 'Untitled', description: doc.description, profile: doc.metadata?.profile, metadata };
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
/** Drop removed node ids from Control Container children so the document stays valid. */
export function pruneContainerChildren(nodes: CanvasNode[], removed: Set<string>): CanvasNode[] {
  return nodes.map((n) => {
    if (n.data.kind !== 'tool' || n.data.tool !== 'core.control_container' || !Array.isArray(n.data.config.children)) return n;
    const children = (n.data.config.children as string[]).filter((id) => !removed.has(id));
    return children.length === (n.data.config.children as string[]).length ? n
      : { ...n, data: { ...n.data, config: { ...n.data.config, children } } } as CanvasNode;
  });
}

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
