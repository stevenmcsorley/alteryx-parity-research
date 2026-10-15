import type { EditorState } from '../state/store';
import { edgeId, nextNodeId, defaultConfig, type CanvasGraph, type CanvasNode } from '../model/workflow';
import { graphOf } from '../state/store';
import { toDocument } from '../model/workflow';
import { engine } from '../state/engineActions';
import type { ValidateResult } from '../engine/types';

export type WorkflowProposal = {
  id: string;
  baseRevision: string;
  candidate: CanvasGraph;
  changes: string[];
  consequences: string[];
  validation: { valid: boolean; problems: { code: string; message: string }[] };
};

type Op = Record<string, unknown> & { op: string };

export function workflowRevision(state: Pick<EditorState, 'nodes' | 'edges' | 'name' | 'description' | 'metadata' | 'profile'>): string {
  const text = JSON.stringify({ nodes: state.nodes.map((n) => ({ id: n.id, data: n.data, position: n.position })), edges: state.edges,
    name: state.name, description: state.description, metadata: state.metadata, profile: state.profile });
  let hash = 2166136261;
  for (let i = 0; i < text.length; i++) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619);
  return `rev_${(hash >>> 0).toString(16).padStart(8, '0')}`;
}

export async function prepareWorkflowProposal(state: EditorState, patchJson: string, baseRevision: string): Promise<WorkflowProposal> {
  if (patchJson.length > 131072) throw new Error('Workflow patch exceeds the 128 KB safety limit');
  if (workflowRevision(state) !== baseRevision) throw new Error('The workflow changed while the proposal was being prepared. Ask Flowbench AI to inspect it again.');
  let parsed: unknown;
  try { parsed = JSON.parse(patchJson); } catch { throw new Error('The proposed patch is not valid JSON'); }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed) || Object.keys(parsed).some((k) => k !== 'operations')) throw new Error('Patch must contain only an operations array');
  const operations = (parsed as { operations?: unknown }).operations;
  if (!Array.isArray(operations) || operations.length < 1) throw new Error('Patch requires at least one operation');

  const graph = structuredClone(graphOf(state));
  const refs = new Map(graph.nodes.map((node, i) => [`node_${i + 1}`, node.id]));
  const changes: string[] = [];
  const consequences: string[] = [];
  for (const raw of operations) {
    const op = validateOperation(raw);
    if (op.op === 'add') {
      const ref = requireString(op.ref, 'ref', 80);
      if (!/^[a-zA-Z][a-zA-Z0-9_-]*$/.test(ref) || refs.has(ref)) throw new Error(`Invalid or duplicate new node ref: ${ref}`);
      const toolId = requireString(op.tool_id, 'tool_id', 160);
      const manifest = state.tools[toolId];
      if (!manifest) throw new Error(`Tool ${toolId} is not installed in this Flowbench engine`);
      const config = requireObject(op.config, 'config');
      assertNoSecretKeys(config);
      if (manifest.side_effects !== 'none') consequences.push(`${manifest.display_name} has declared effects: ${manifest.side_effects}`);
      const pos = requirePosition(op.position);
      const id = nextNodeId(toolId, graph.nodes.map((n) => n.id));
      const defaults = defaultConfig(manifest.config_schema);
      const finalConfig = { ...defaults, ...structuredClone(config) };
      if (toolId === 'core.input_data' && !('type_inference' in config)) finalConfig.type_inference = 'flowbench';
      const node: CanvasNode = { id, type: 'tool', position: pos, data: { kind: 'tool', tool: toolId, config: finalConfig } };
      graph.nodes.push(node); refs.set(ref, id); changes.push(`Add ${manifest.display_name} (${ref}) · ${JSON.stringify(redactConfig(finalConfig)).slice(0, 600)}`);
    } else if (op.op === 'configure') {
      const ref = requireString(op.node_ref, 'node_ref', 80);
      const id = refs.get(ref); const node = graph.nodes.find((n) => n.id === id);
      if (!node || node.data.kind !== 'tool') throw new Error(`Unknown tool node reference: ${ref}`);
      const config = requireObject(op.config, 'config');
      assertNoSecretKeys(config);
      const manifest = state.tools[node.data.tool];
      if (!manifest) throw new Error(`Tool ${node.data.tool} has no installed manifest`);
      if (manifest.side_effects !== 'none') consequences.push(`${manifest.display_name} has declared effects: ${manifest.side_effects}`);
      node.data = { ...node.data, config: { ...node.data.config, ...structuredClone(config) } };
      changes.push(`Configure ${manifest.display_name} (${ref}) · ${JSON.stringify(redactConfig(config)).slice(0, 600)}`);
    } else if (op.op === 'connect') {
      const fromRef = requireString(op.source_ref, 'source_ref', 80), toRef = requireString(op.target_ref, 'target_ref', 80);
      const source = refs.get(fromRef), target = refs.get(toRef);
      if (!source || !target) throw new Error('Connection refers to an unknown node');
      const sourceAnchor = requireString(op.source_anchor, 'source_anchor', 80), targetAnchor = requireString(op.target_anchor, 'target_anchor', 80);
      const sourceNode = graph.nodes.find((n) => n.id === source), targetNode = graph.nodes.find((n) => n.id === target);
      if (sourceNode?.data.kind !== 'tool' || targetNode?.data.kind !== 'tool') throw new Error('Only compatible tool nodes can be connected by this operation');
      if (!state.tools[sourceNode.data.tool]?.outputs.some((a) => a.name === sourceAnchor)) throw new Error(`Unknown output anchor ${sourceAnchor}`);
      const input = state.tools[targetNode.data.tool]?.inputs.find((a) => a.name === targetAnchor);
      if (!input) throw new Error(`Unknown input anchor ${targetAnchor}`);
      if (!input.multiple && graph.edges.some((e) => e.target === target && e.targetHandle === targetAnchor)) throw new Error(`Input ${targetAnchor} already has a connection`);
      const id = edgeId(source, sourceAnchor, target, targetAnchor);
      if (graph.edges.some((e) => e.id === id)) throw new Error('Connection already exists');
      graph.edges.push({ id, source, sourceHandle: sourceAnchor, target, targetHandle: targetAnchor });
      changes.push(`Connect ${fromRef}.${sourceAnchor} → ${toRef}.${targetAnchor}`);
    } else if (op.op === 'disconnect') {
      const fromRef = requireString(op.source_ref, 'source_ref', 80), toRef = requireString(op.target_ref, 'target_ref', 80);
      const source = refs.get(fromRef), target = refs.get(toRef);
      const sourceAnchor = requireString(op.source_anchor, 'source_anchor', 80), targetAnchor = requireString(op.target_anchor, 'target_anchor', 80);
      const before = graph.edges.length;
      graph.edges = graph.edges.filter((e) => !(e.source === source && e.target === target && e.sourceHandle === sourceAnchor && e.targetHandle === targetAnchor));
      if (before === graph.edges.length) throw new Error('No matching connection exists to disconnect');
      changes.push(`Disconnect ${fromRef}.${sourceAnchor} → ${toRef}.${targetAnchor}`);
    } else if (op.op === 'remove') {
      const ref = requireString(op.node_ref, 'node_ref', 80), id = refs.get(ref);
      if (!id) throw new Error(`Unknown node reference ${ref}`);
      const target = graph.nodes.find((node) => node.id === id)!;
      if (target.data.kind === 'application-action') throw new Error('AI removal of Interface Action nodes is not yet supported; their app metadata needs a dedicated transactional adapter');
      const removing = new Set([id]);
      let expanded = true;
      while (expanded) {
        expanded = false;
        for (const node of graph.nodes) if (node.parentId && removing.has(node.parentId) && !removing.has(node.id)) { removing.add(node.id); expanded = true; }
      }
      if (removing.size > 1) consequences.push(`Removing ${ref} also removes ${removing.size - 1} contained node(s)`);
      consequences.push(`Removing ${ref} also removes its connected edges`);
      graph.nodes = graph.nodes.filter((n) => !removing.has(n.id));
      graph.edges = graph.edges.filter((e) => !removing.has(e.source) && !removing.has(e.target));
      for (const [nodeRef, nodeId] of refs) if (removing.has(nodeId)) refs.delete(nodeRef);
      changes.push(`Remove ${ref} and its connections`);
    }
  }
  const result = await engine().request<ValidateResult>('workflow.validate', { workflow: toDocument(graph), base_dir: state.baseDir, profile: state.profile });
  return { id: crypto.randomUUID(), baseRevision, candidate: graph, changes, consequences: [...new Set(consequences)],
    validation: { valid: result.valid, problems: result.problems.map((p) => ({ code: p.code, message: p.message.slice(0, 240) })) } };
}

function validateOperation(value: unknown): Op {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Each patch operation must be an object');
  const op = value as Op;
  const fields: Record<string, string[]> = {
    add: ['op', 'ref', 'tool_id', 'config', 'position'], configure: ['op', 'node_ref', 'config'],
    connect: ['op', 'source_ref', 'source_anchor', 'target_ref', 'target_anchor'],
    disconnect: ['op', 'source_ref', 'source_anchor', 'target_ref', 'target_anchor'], remove: ['op', 'node_ref'],
  };
  const allowed = fields[op.op];
  if (!allowed || Object.keys(op).length !== allowed.length || Object.keys(op).some((key) => !allowed.includes(key))) throw new Error('Unsupported patch operation or unexpected operation fields');
  return op;
}
function requireString(value: unknown, name: string, max: number): string {
  if (typeof value !== 'string' || !value.trim() || value.length > max) throw new Error(`${name} must be a non-empty string of at most ${max} characters`);
  return value;
}
function requireObject(value: unknown, name: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value) || JSON.stringify(value).length > 65536) throw new Error(`${name} must be an object no larger than 64 KB`);
  return value as Record<string, unknown>;
}
function requirePosition(value: unknown): { x: number; y: number } {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('position must be an object');
  const p = value as Record<string, unknown>;
  if (Object.keys(p).length !== 2 || !Number.isFinite(p.x) || !Number.isFinite(p.y) || Math.abs(Number(p.x)) > 10000 || Math.abs(Number(p.y)) > 10000) throw new Error('position requires bounded finite x/y coordinates');
  return { x: Number(p.x), y: Number(p.y) };
}
function redactConfig(value: unknown, key = ''): unknown {
  if (/pass(word|wd)?|secret|token|credential|api.?key|connection|private.?key|access.?key|authorization/i.test(key)) return '[redacted]';
  if (typeof value === 'string') return value.length > 200 ? `${value.slice(0, 200)}…` : value;
  if (Array.isArray(value)) return value.slice(0, 20).map((item) => redactConfig(item, key));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).slice(0, 40).map(([k, v]) => [k, redactConfig(v, k)]));
  return value;
}
function assertNoSecretKeys(value: unknown): void {
  if (!value || typeof value !== 'object') return;
  if (Array.isArray(value)) { value.forEach(assertNoSecretKeys); return; }
  for (const [key, child] of Object.entries(value)) {
    if (/pass(word|wd)?|secret|token|credential|api.?key|connection.?string|private.?key|access.?key|authorization/i.test(key))
      throw new Error('AI proposals cannot set credential or secret fields; configure them through Flowbench’s protected connection settings');
    assertNoSecretKeys(child);
  }
}
