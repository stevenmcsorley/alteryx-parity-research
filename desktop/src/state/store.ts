import { create } from 'zustand';
import { applyEdgeChanges, applyNodeChanges, type Connection, type EdgeChange, type NodeChange } from '@xyflow/react';
import type { CatalogEntry, EngineEvent, FieldDef, Problem, Profile, RunPayload, ToolManifest, HistoryEntry } from '../engine/types';
import { defaultConfig, edgeId, nextNodeId, type CanvasEdge, type CanvasNode, type CanvasGraph } from '../model/workflow';

type Snapshot = { nodes: CanvasNode[]; edges: CanvasEdge[]; name: string; description?: string; metadata?: Record<string, any> };

export type RunState = {
  runId: string | null;
  status: 'idle' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'invalid';
  events: EngineEvent[];
  nodeStatus: Record<string, 'queued' | 'running' | 'done' | 'failed' | 'skipped'>;
  nodeRows: Record<string, Record<string, number>>; // node -> anchor -> rows
  nodeMs: Record<string, number>;
  result: RunPayload | null;
  startedAt: number | null;
};

const emptyRun: RunState = { runId: null, status: 'idle', events: [], nodeStatus: {}, nodeRows: {}, nodeMs: {}, result: null, startedAt: null };

export type EditorState = {
  // document
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  name: string;
  description?: string;
  metadata?: Record<string, any>;
  profile: string;
  filePath: string | null;
  baseDir: string;
  dirty: boolean;
  past: Snapshot[];
  future: Snapshot[];
  clipboard: Snapshot | null;
  // engine metadata
  tools: Record<string, ToolManifest>;
  catalog: CatalogEntry[];
  profiles: Profile[];
  engineInfo: { version: string; python: string; protocol: number } | null;
  engineError: string | null;
  // analysis
  problems: Problem[];
  schemas: Record<string, Record<string, FieldDef[]>>;
  validating: boolean;
  // run
  run: RunState;
  history: HistoryEntry[];
  // ui
  selectedResult: { node: string; anchor: string } | null;
  bottomTab: 'results' | 'fields' | 'messages' | 'performance' | 'history';
  loadVersion: number; // increments when a document is loaded/created (canvas re-fits)
  importReport: any | null; // report from importing a .yxmd/.yxmc/.yxwz/.yxzp document

  // actions
  setEngineMeta(m: Partial<Pick<EditorState, 'tools' | 'catalog' | 'profiles' | 'engineInfo' | 'engineError'>>): void;
  loadGraph(g: CanvasGraph, filePath?: string | null, baseDir?: string): void;
  newWorkflow(): void;
  checkpoint(): void;
  undo(): void;
  redo(): void;
  onNodesChange(changes: NodeChange<CanvasNode>[]): void;
  onEdgesChange(changes: EdgeChange<CanvasEdge>[]): void;
  connect(c: Connection): string | null;
  addTool(toolId: string, position: { x: number; y: number }): string;
  addApplicationAction(position: { x: number; y: number }, questionType?: string, prompt?: string): string;
  addApplicationCondition(position: { x: number; y: number }): string;
  addApplicationError(position: { x: number; y: number }): string;
  addComment(position: { x: number; y: number }): string;
  updateNodeData(id: string, patch: Record<string, unknown>, record?: boolean): void;
  deleteSelected(): void;
  selectAll(): void;
  copySelected(): void;
  paste(): void;
  duplicateSelected(): void;
  groupSelected(caption?: string): string | null;
  ungroup(containerId: string): void;
  commitGraphTransaction(graph: CanvasGraph): void;
  setName(name: string): void;
  setProfile(p: string): void;
  setFilePath(p: string | null, baseDir?: string): void;
  markSaved(): void;
  setValidation(problems: Problem[], schemas: EditorState['schemas']): void;
  setValidating(v: boolean): void;
  runStarted(runId: string): void;
  runEvent(ev: EngineEvent): void;
  runFinished(result: RunPayload): void;
  runInvalid(problems: Problem[]): void;
  setHistory(h: HistoryEntry[]): void;
  selectResult(node: string, anchor: string): void;
  setBottomTab(t: EditorState['bottomTab']): void;
};

const MAX_HISTORY = 200;

function snap(s: EditorState): Snapshot {
  return structuredClone({ nodes: s.nodes.map(stripRuntime), edges: s.edges.map(stripEdge), name: s.name, description: s.description, metadata: s.metadata });
}
function stripRuntime(n: CanvasNode): CanvasNode {
  const { selected: _s, dragging: _d, measured: _m, ...rest } = n as any;
  return rest;
}
function stripEdge(e: CanvasEdge): CanvasEdge {
  const { selected: _s, ...rest } = e as any;
  return rest;
}

export const useEditor = create<EditorState>((set, get) => ({
  nodes: [],
  edges: [],
  name: 'Untitled workflow',
  metadata: undefined,
  profile: 'ayx-2026.2-e1',
  filePath: null,
  baseDir: '',
  dirty: false,
  past: [],
  future: [],
  clipboard: null,
  tools: {},
  catalog: [],
  profiles: [],
  engineInfo: null,
  engineError: null,
  problems: [],
  schemas: {},
  validating: false,
  run: emptyRun,
  history: [],
  selectedResult: null,
  bottomTab: 'results',
  loadVersion: 0,
  importReport: null,

  setEngineMeta: (m) => set(m as any),

  loadGraph: (g, filePath = null, baseDir) =>
    set({ nodes: g.nodes, edges: g.edges, name: g.name, description: g.description, metadata: g.metadata, profile: g.profile ?? get().profile,
      filePath, baseDir: baseDir ?? get().baseDir, dirty: false, past: [], future: [], run: emptyRun, problems: [],
      schemas: {}, selectedResult: null, loadVersion: get().loadVersion + 1, importReport: null }),

  newWorkflow: () => set({ nodes: [], edges: [], name: 'Untitled workflow', description: undefined, metadata: undefined, filePath: null,
    dirty: false, past: [], future: [], run: emptyRun, problems: [], schemas: {}, selectedResult: null,
    loadVersion: get().loadVersion + 1, importReport: null }),

  checkpoint: () => {
    const s = get();
    set({ past: [...s.past.slice(-(MAX_HISTORY - 1)), snap(s)], future: [], dirty: true });
  },

  undo: () => {
    const s = get();
    const prev = s.past[s.past.length - 1];
    if (!prev) return;
    set({ ...structuredClone(prev), past: s.past.slice(0, -1), future: [snap(s), ...s.future], dirty: true });
  },

  redo: () => {
    const s = get();
    const next = s.future[0];
    if (!next) return;
    set({ ...structuredClone(next), past: [...s.past, snap(s)], future: s.future.slice(1), dirty: true });
  },

  onNodesChange: (changes) => {
    // record history once at drag start and for removals/resizes; pure selection/measurement changes are not edits
    const editing = changes.some((c) => (c.type === 'position' && c.dragging) || c.type === 'remove' ||
      (c.type === 'dimensions' && (c as any).resizing));
    const s = get();
    const dragStart = changes.some((c) => c.type === 'position' && c.dragging && !s.nodes.find((n) => n.id === c.id)?.dragging);
    if (editing && (dragStart || changes.some((c) => c.type === 'remove'))) get().checkpoint();
    set({ nodes: applyNodeChanges(changes, get().nodes) });
  },

  onEdgesChange: (changes) => {
    if (changes.some((c) => c.type === 'remove')) get().checkpoint();
    set({ edges: applyEdgeChanges(changes, get().edges) });
  },

  connect: (c) => {
    const s = get();
    if (!c.source || !c.target) return 'Incomplete connection';
    if (c.source === c.target) return 'A tool cannot connect to itself';
    const target = s.nodes.find((n) => n.id === c.target);
    const source = s.nodes.find((n) => n.id === c.source);
    if (source?.data.kind === 'application-condition') {
      if (!['application-action', 'application-error'].includes(target?.data.kind ?? '') || !['True Condition', 'False Condition'].includes(c.sourceHandle ?? '') || c.targetHandle !== 'Condition')
        return 'Condition outputs must connect to an Action or Error Message Condition input';
      if (target?.data.kind === 'application-action' && target.data.question.type === 'control-parameter')
        return 'Control Parameters cannot be conditionally routed; configure the parent control records instead';
      const id = edgeId(c.source, c.sourceHandle!, c.target, 'Condition');
      if (s.edges.some((e) => e.id === id)) return 'Already connected';
      if (s.edges.some((e) => e.target === c.target && e.data?.kind === 'application-condition'))
        return 'A target can have only one Condition route';
      get().checkpoint();
      set({ edges: [...s.edges, { id, source: c.source, sourceHandle: c.sourceHandle!, target: c.target,
        targetHandle: 'Condition', data: { kind: 'application-condition' },
        style: { stroke: '#dd8a36', strokeDasharray: '4 4' }, label: c.sourceHandle === 'True Condition' ? 'True' : 'False' }] });
      return null;
    }
    if (source?.data.kind === 'application-action') {
      if (target?.data.kind !== 'tool' || c.sourceHandle !== 'Action' || c.targetHandle !== '__action__')
        return 'Action connections must run from the Action anchor to a tool Action target';
      const id = edgeId(c.source, 'Action', c.target, '__action__');
      if (s.edges.some((e) => e.id === id)) return 'Already connected';
      if (s.edges.some((e) => e.target === c.target && e.targetHandle === '__action__'))
        return 'A tool can have only one native Action connection in this editor';
      get().checkpoint();
      set({ edges: [...s.edges, { id, source: c.source, sourceHandle: 'Action', target: c.target,
        targetHandle: '__action__', data: { kind: 'application-action' },
        style: { stroke: '#805ad5', strokeDasharray: '5 4' }, label: 'Action' }] });
      return null;
    }
    if (target?.data.kind === 'application-action' || target?.data.kind === 'application-condition' ||
        source?.data.kind !== 'tool' || target?.data.kind !== 'tool')
      return 'Only data tools connect through row-data anchors';
    const manifest = s.tools[target.data.tool];
    const anchor = manifest?.inputs.find((a) => a.name === c.targetHandle);
    if (!anchor) return `Unknown input anchor ${c.targetHandle}`;
    const occupied = s.edges.some((e) => e.target === c.target && e.targetHandle === c.targetHandle);
    if (occupied && !anchor.multiple) return `Input "${anchor.label}" accepts one connection`;
    const id = edgeId(c.source, c.sourceHandle ?? 'output', c.target, c.targetHandle ?? 'input');
    if (s.edges.some((e) => e.id === id)) return 'Already connected';
    // reject cycles
    const reach = new Set<string>([c.target]);
    let grew = true;
    while (grew) {
      grew = false;
      for (const e of s.edges) if (reach.has(e.source) && !reach.has(e.target)) { reach.add(e.target); grew = true; }
    }
    if (reach.has(c.source)) return 'This connection would create a cycle';
    get().checkpoint();
    set({ edges: [...s.edges, { id, source: c.source, sourceHandle: c.sourceHandle ?? 'output', target: c.target,
      targetHandle: c.targetHandle ?? 'input' }] });
    return null;
  },

  addTool: (toolId, position) => {
    const s = get();
    const m = s.tools[toolId];
    const id = nextNodeId(toolId, s.nodes.map((n) => n.id));
    get().checkpoint();
    const config = m ? defaultConfig(m.config_schema) : {};
    // New Flowbench Input Data nodes opt into the modern inference policy.
    // Legacy native workflows and imported Alteryx workflows keep their saved
    // all-text behavior unless the user changes this setting explicitly.
    if (toolId === 'core.input_data') config.type_inference = 'flowbench';
    set({ nodes: [...s.nodes.map((n) => ({ ...n, selected: false })), { id, type: 'tool', position, selected: true,
      data: { kind: 'tool', tool: toolId, config } }] });
    return id;
  },

  addApplicationAction: (position, questionType = 'text', prompt = 'Enter a value') => {
    const s = get();
    const id = nextNodeId('application_action', s.nodes.map((n) => n.id));
    const used = new Set<string>([
      ...(s.metadata?.application?.questions ?? []).map((q: any) => String(q.id)),
      ...s.nodes.filter((n) => n.data.kind === 'application-action').map((n) => (n.data as any).question.id as string),
    ]);
    const questionId = nextNodeId('question', used);
    get().checkpoint();
    set({ nodes: [...s.nodes.map((n) => ({ ...n, selected: false })), { id, type: 'applicationAction', position,
      selected: true, data: { kind: 'application-action', question: { id: questionId, prompt,
        type: (questionType === 'control-parameter' ? 'control-parameter' : questionType === 'check-box' ? 'boolean' : questionType === 'numeric-up-down' ? 'number' : questionType === 'drop-down' || questionType === 'radio-button' ? 'choice' : questionType === 'date' ? 'date' : questionType === 'file-browse' ? 'file' : questionType === 'folder-browse' ? 'folder' : questionType === 'list-box' || questionType === 'check-box' ? 'multi-choice' : 'text') as any,
        ...(questionType === 'control-parameter' ? { control_field: '' } : {}),
        ...(['drop-down', 'radio-button'].includes(questionType) ? { options: ['Option 1', 'Option 2'] } : questionType === 'list-box' ? { type: 'multi-choice' as const, options: ['Option 1', 'Option 2'] } : {}),
        ...(questionType === 'numeric-up-down' ? { minimum: 0, maximum: 100, default: 0 } : questionType === 'check-box' ? { default: false } : {}) },
        config_path: ['expression'] } }] });
    return id;
  },

  addApplicationCondition: (position) => {
    const s = get();
    const id = nextNodeId('condition', s.nodes.map((n) => n.id));
    const firstQuestion = s.nodes.find((n) => n.data.kind === 'application-action')?.data;
    const questionId = firstQuestion?.kind === 'application-action' ? firstQuestion.question.id : '';
    get().checkpoint();
    set({ nodes: [...s.nodes.map((n) => ({ ...n, selected: false })), { id, type: 'applicationCondition', position,
      selected: true, data: { kind: 'application-condition', when: { question: questionId, operator: 'truthy' } } }] });
    return id;
  },

  addApplicationError: (position) => {
    const s = get();
    const id = nextNodeId('error_message', s.nodes.map((n) => n.id));
    get().checkpoint();
    set({ nodes: [...s.nodes.map((n) => ({ ...n, selected: false })), { id, type: 'applicationError', position,
      selected: true, data: { kind: 'application-error', message: 'Please correct the application inputs before running.' } }] });
    return id;
  },

  addComment: (position) => {
    const s = get();
    const id = nextNodeId('comment', s.nodes.map((n) => n.id));
    get().checkpoint();
    set({ nodes: [...s.nodes, { id, type: 'comment', position, data: { kind: 'comment', text: 'Comment' },
      style: { width: 200, height: 80 } }] });
    return id;
  },

  updateNodeData: (id, patch, record = true) => {
    if (record) get().checkpoint();
    set({ nodes: get().nodes.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } as any } : n)) });
  },

  deleteSelected: () => {
    const s = get();
    const sel = new Set(s.nodes.filter((n) => n.selected).map((n) => n.id));
    const selEdges = new Set(s.edges.filter((e) => e.selected).map((e) => e.id));
    if (!sel.size && !selEdges.size) return;
    // deleting a container deletes its contents
    let grew = true;
    while (grew) {
      grew = false;
      for (const n of s.nodes) if (n.parentId && sel.has(n.parentId) && !sel.has(n.id)) { sel.add(n.id); grew = true; }
    }
    get().checkpoint();
    set({ nodes: s.nodes.filter((n) => !sel.has(n.id)),
      edges: s.edges.filter((e) => !selEdges.has(e.id) && !sel.has(e.source) && !sel.has(e.target)) });
  },

  selectAll: () => set({ nodes: get().nodes.map((n) => ({ ...n, selected: true })) }),

  copySelected: () => {
    const s = get();
    const ids = new Set(s.nodes.filter((n) => n.selected).map((n) => n.id));
    if (!ids.size) return;
    set({ clipboard: structuredClone({ nodes: s.nodes.filter((n) => ids.has(n.id)).map(stripRuntime),
      edges: s.edges.filter((e) => ids.has(e.source) && ids.has(e.target)).map(stripEdge), name: s.name }) });
  },

  paste: () => {
    const s = get();
    if (!s.clipboard) return;
    const taken = s.nodes.map((n) => n.id);
    const map = new Map<string, string>();
    for (const n of s.clipboard.nodes) {
      const prefix = n.data.kind === 'tool' ? n.data.tool : n.data.kind;
      const nid = nextNodeId(prefix, [...taken, ...map.values()]);
      map.set(n.id, nid);
    }
    const nodes = s.clipboard.nodes.map((n) => ({
      ...structuredClone(n), id: map.get(n.id)!, selected: true,
      parentId: n.parentId && map.has(n.parentId) ? map.get(n.parentId) : undefined,
      position: n.parentId && map.has(n.parentId) ? n.position : { x: n.position.x + 40, y: n.position.y + 40 },
    }));
    for (const n of nodes) if (!n.parentId) delete n.parentId;
    const edges = s.clipboard.edges.map((e) => {
      const source = map.get(e.source)!, target = map.get(e.target)!;
      return { ...e, id: edgeId(source, e.sourceHandle!, target, e.targetHandle!), source, target };
    });
    get().checkpoint();
    set({ nodes: [...s.nodes.map((n) => ({ ...n, selected: false })), ...nodes], edges: [...s.edges, ...edges] });
  },

  duplicateSelected: () => {
    get().copySelected();
    get().paste();
  },

  groupSelected: (caption = 'Container') => {
    const s = get();
    const sel = s.nodes.filter((n) => n.selected && !n.parentId);
    if (!sel.length) return null;
    const w = (n: CanvasNode) => Number(n.measured?.width ?? n.width ?? (n.style as any)?.width ?? 180);
    const h = (n: CanvasNode) => Number(n.measured?.height ?? n.height ?? (n.style as any)?.height ?? 64);
    const minX = Math.min(...sel.map((n) => n.position.x)) - 30;
    const minY = Math.min(...sel.map((n) => n.position.y)) - 50;
    const maxX = Math.max(...sel.map((n) => n.position.x + w(n))) + 30;
    const maxY = Math.max(...sel.map((n) => n.position.y + h(n))) + 30;
    const id = nextNodeId('container', s.nodes.map((n) => n.id));
    const container: CanvasNode = { id, type: 'container', position: { x: minX, y: minY },
      data: { kind: 'container', caption }, style: { width: maxX - minX, height: maxY - minY } };
    const ids = new Set(sel.map((n) => n.id));
    get().checkpoint();
    set({ nodes: [container, ...s.nodes.map((n) => ids.has(n.id)
      ? { ...n, parentId: id, position: { x: n.position.x - minX, y: n.position.y - minY }, selected: false } : n)] });
    return id;
  },

  ungroup: (cid) => {
    const s = get();
    const c = s.nodes.find((n) => n.id === cid);
    if (!c) return;
    get().checkpoint();
    set({ nodes: s.nodes.filter((n) => n.id !== cid).map((n) => {
      if (n.parentId !== cid) return n;
      const rest = { ...n, parentId: c.parentId, position: { x: n.position.x + c.position.x, y: n.position.y + c.position.y } };
      if (!c.parentId) delete rest.parentId;
      return rest as CanvasNode;
    }) });
  },

  commitGraphTransaction: (graph) => {
    // The approved AI patch becomes one ordinary editor transaction and one undo step.
    get().checkpoint();
    set({ nodes: structuredClone(graph.nodes), edges: structuredClone(graph.edges), name: graph.name,
      description: graph.description, metadata: structuredClone(graph.metadata), dirty: true,
      problems: [], schemas: {}, run: emptyRun, selectedResult: null });
  },

  setName: (name) => { get().checkpoint(); set({ name }); },
  setProfile: (profile) => set({ profile, dirty: true }),
  setFilePath: (filePath, baseDir) => set({ filePath, baseDir: baseDir ?? get().baseDir }),
  markSaved: () => set({ dirty: false }),
  setValidation: (problems, schemas) => set({ problems, schemas, validating: false }),
  setValidating: (validating) => set({ validating }),

  runStarted: (runId) => set({ run: { ...emptyRun, runId, status: 'running', startedAt: Date.now() }, selectedResult: null }),

  runEvent: (ev) => {
    const r = get().run;
    if (!r.runId || ev.run_id !== r.runId) return;
    const nodeStatus = { ...r.nodeStatus };
    const nodeRows = { ...r.nodeRows };
    const nodeMs = { ...r.nodeMs };
    if (ev.node_id) {
      if (ev.type === 'node-started') nodeStatus[ev.node_id] = 'running';
      if (ev.type === 'node-completed') {
        nodeStatus[ev.node_id] = 'done';
        if (ev.output_rows) nodeRows[ev.node_id] = ev.output_rows;
        if (typeof ev.duration_ms === 'number') nodeMs[ev.node_id] = ev.duration_ms;
      }
      if (ev.type === 'node-failed') nodeStatus[ev.node_id] = 'failed';
      if (ev.type === 'node-skipped') nodeStatus[ev.node_id] = 'skipped';
    }
    set({ run: { ...r, events: [...r.events, ev], nodeStatus, nodeRows, nodeMs } });
  },

  runFinished: (result) => {
    const r = get().run;
    if (!r.runId || result.run_id !== r.runId) return; // stale result from a run this document did not start
    // the final payload carries the authoritative event list; rebuild per-node state from it
    const nodeStatus: RunState['nodeStatus'] = {};
    const nodeRows: RunState['nodeRows'] = {};
    const nodeMs: RunState['nodeMs'] = {};
    for (const ev of result.events ?? []) {
      if (!ev.node_id) continue;
      if (ev.type === 'node-completed') {
        nodeStatus[ev.node_id] = 'done';
        if (ev.output_rows) nodeRows[ev.node_id] = ev.output_rows;
        if (typeof ev.duration_ms === 'number') nodeMs[ev.node_id] = ev.duration_ms;
      } else if (ev.type === 'node-failed') nodeStatus[ev.node_id] = 'failed';
      else if (ev.type === 'node-skipped') nodeStatus[ev.node_id] = 'skipped';
    }
    const firstPreview = Object.entries(result.previews ?? {}).find(([, a]) => Object.keys(a).length);
    set({ run: { ...r, status: result.status as RunState['status'], events: result.events ?? r.events, nodeStatus, nodeRows, nodeMs, result },
      selectedResult: get().selectedResult ?? (firstPreview ? { node: firstPreview[0], anchor: Object.keys(firstPreview[1])[0] } : null),
      bottomTab: result.status === 'failed' ? 'messages' : get().bottomTab });
  },

  runInvalid: (problems) => set({ run: { ...emptyRun, status: 'invalid' }, problems, bottomTab: 'messages' }),
  setHistory: (history) => set({ history }),
  selectResult: (node, anchor) => set({ selectedResult: { node, anchor }, bottomTab: 'results' }),
  setBottomTab: (bottomTab) => set({ bottomTab }),
}));

export function graphOf(s: EditorState): CanvasGraph {
  return { nodes: s.nodes, edges: s.edges, name: s.name, description: s.description, profile: s.profile, metadata: s.metadata };
}
