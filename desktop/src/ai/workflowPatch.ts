import type { EditorState } from '../state/store';
import { edgeId, nextNodeId, defaultConfig, type ApplicationActionNodeData, type ApplicationPredicate, type CanvasGraph, type CanvasNode } from '../model/workflow';
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
  const text = JSON.stringify({ nodes: state.nodes.map((n) => ({ id: n.id, data: n.data, position: n.position,
    parentId: n.parentId ?? null, width: n.width ?? null, height: n.height ?? null, style: n.style ?? null })), edges: state.edges,
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
    } else if (op.op === 'add_question') {
      const ref = requireRef(op.ref, refs);
      const question = validateQuestion(op.question);
      const known = new Set(graph.nodes.filter((n) => n.data.kind === 'application-action').map((n) => (n.data as ApplicationActionNodeData).question.id));
      if (known.has(question.id)) throw new Error(`Application question id ${question.id} already exists`);
      const path = validateConfigPath(op.config_path);
      const id = nextNodeId('application_action', graph.nodes.map((n) => n.id));
      graph.nodes.push({ id, type: 'applicationAction', position: requirePosition(op.position), data: { kind: 'application-action', question, config_path: path } });
      refs.set(ref, id); changes.push(`Add ${question.type} application question ${question.prompt} (${ref})`);
    } else if (op.op === 'add_condition') {
      const ref = requireRef(op.ref, refs);
      const when = validatePredicate(op.when);
      const id = nextNodeId('condition', graph.nodes.map((n) => n.id));
      graph.nodes.push({ id, type: 'applicationCondition', position: requirePosition(op.position), data: { kind: 'application-condition', when } });
      refs.set(ref, id); changes.push(`Add Condition (${ref}) · ${when.question} ${when.operator}`);
    } else if (op.op === 'add_error_message') {
      const ref = requireString(op.ref, 'ref', 80);
      if (!/^[a-zA-Z][a-zA-Z0-9_-]*$/.test(ref) || refs.has(ref)) throw new Error(`Invalid or duplicate new node ref: ${ref}`);
      const message = requireString(op.message, 'message', 4000);
      const id = nextNodeId('error_message', graph.nodes.map((n) => n.id));
      graph.nodes.push({ id, type: 'applicationError', position: requirePosition(op.position), data: { kind: 'application-error', message } });
      refs.set(ref, id); changes.push(`Add Error Message (${ref})`);
    } else if (op.op === 'add_comment') {
      const ref = requireString(op.ref, 'ref', 80);
      if (!/^[a-zA-Z][a-zA-Z0-9_-]*$/.test(ref) || refs.has(ref)) throw new Error(`Invalid or duplicate new node ref: ${ref}`);
      const text = requireString(op.text, 'text', 4000);
      const id = nextNodeId('comment', graph.nodes.map((n) => n.id));
      graph.nodes.push({ id, type: 'comment', position: requirePosition(op.position), data: { kind: 'comment', text }, style: { width: 200, height: 80 } });
      refs.set(ref, id); changes.push(`Add Comment (${ref})`);
    } else if (op.op === 'group') {
      const ref = requireRef(op.ref, refs);
      const caption = requireString(op.caption, 'caption', 120);
      if (!Array.isArray(op.node_refs) || op.node_refs.length < 1 || op.node_refs.length > graph.nodes.length)
        throw new Error('Tool Container requires one or more existing node references');
      const ids = op.node_refs.map((value) => {
        if (typeof value !== 'string' || !refs.has(value)) throw new Error(`Unknown node reference in Tool Container: ${String(value)}`);
        return refs.get(value)!;
      });
      if (new Set(ids).size !== ids.length) throw new Error('Tool Container node_refs must be unique');
      const members = ids.map((id) => graph.nodes.find((node) => node.id === id)!);
      if (members.some((node) => !!node.parentId)) throw new Error('AI Tool Container grouping currently accepts top-level nodes only');
      const width = (node: CanvasNode) => Number(node.measured?.width ?? node.width ?? (node.style as any)?.width ?? (node.data.kind === 'comment' ? 200 : 180));
      const height = (node: CanvasNode) => Number(node.measured?.height ?? node.height ?? (node.style as any)?.height ?? (node.data.kind === 'comment' ? 80 : 64));
      const minX = Math.min(...members.map((node) => node.position.x)) - 30;
      const minY = Math.min(...members.map((node) => node.position.y)) - 50;
      const maxX = Math.max(...members.map((node) => node.position.x + width(node))) + 30;
      const maxY = Math.max(...members.map((node) => node.position.y + height(node))) + 30;
      const id = nextNodeId('container', graph.nodes.map((node) => node.id));
      graph.nodes.push({ id, type: 'container', position: { x: minX, y: minY },
        data: { kind: 'container', caption }, style: { width: maxX - minX, height: maxY - minY } });
      for (const member of members) {
        member.parentId = id;
        member.position = { x: member.position.x - minX, y: member.position.y - minY };
        member.selected = false;
      }
      refs.set(ref, id);
      changes.push(`Group ${members.length} node(s) in Tool Container “${caption}” (${ref})`);
    } else if (op.op === 'configure_container') {
      const ref = requireString(op.node_ref, 'node_ref', 80), id = refs.get(ref);
      const node = graph.nodes.find((n) => n.id === id);
      if (node?.data.kind !== 'container') throw new Error(`Unknown Tool Container reference: ${ref}`);
      const caption = requireString(op.caption, 'caption', 120);
      if (typeof op.disabled !== 'boolean' || typeof op.collapsed !== 'boolean') throw new Error('Tool Container disabled and collapsed values must be booleans');
      if (node.data.disabled !== op.disabled) consequences.push(`${op.disabled ? 'Disable' : 'Enable'} Tool Container ${ref}; contained tools will ${op.disabled ? 'not execute' : 'be eligible to execute'}`);
      node.data = { ...node.data, caption, disabled: op.disabled, collapsed: op.collapsed };
      changes.push(`Configure Tool Container (${ref}) · ${caption}`);
    } else if (op.op === 'ungroup_container') {
      const ref = requireString(op.node_ref, 'node_ref', 80), id = refs.get(ref);
      const container = graph.nodes.find((n) => n.id === id);
      if (container?.data.kind !== 'container') throw new Error(`Unknown Tool Container reference: ${ref}`);
      graph.nodes = graph.nodes.filter((n) => n.id !== id).map((node) => {
        if (node.parentId !== id) return node;
        return { ...node, parentId: container.parentId, position: { x: node.position.x + container.position.x, y: node.position.y + container.position.y } };
      });
      refs.delete(ref);
      changes.push(`Ungroup Tool Container (${ref})`);
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
    } else if (op.op === 'configure_question') {
      const ref = requireString(op.node_ref, 'node_ref', 80), id = refs.get(ref);
      const node = graph.nodes.find((n) => n.id === id);
      if (node?.data.kind !== 'application-action') throw new Error(`Unknown application question reference: ${ref}`);
      const question = validateQuestion(op.question);
      const path = validateConfigPath(op.config_path);
      node.data = { ...node.data, question, config_path: path };
      changes.push(`Configure application question ${question.prompt} (${ref})`);
    } else if (op.op === 'configure_condition') {
      const ref = requireString(op.node_ref, 'node_ref', 80), id = refs.get(ref);
      const node = graph.nodes.find((n) => n.id === id);
      if (node?.data.kind !== 'application-condition') throw new Error(`Unknown Condition reference: ${ref}`);
      const when = validatePredicate(op.when);
      node.data = { ...node.data, when };
      changes.push(`Configure Condition (${ref}) · ${when.question} ${when.operator}`);
    } else if (op.op === 'configure_error_message') {
      const ref = requireString(op.node_ref, 'node_ref', 80), id = refs.get(ref);
      const node = graph.nodes.find((n) => n.id === id);
      if (node?.data.kind !== 'application-error') throw new Error(`Unknown Error Message reference: ${ref}`);
      const message = requireString(op.message, 'message', 4000);
      node.data = { ...node.data, message };
      changes.push(`Configure Error Message (${ref})`);
    } else if (op.op === 'configure_comment') {
      const ref = requireString(op.node_ref, 'node_ref', 80), id = refs.get(ref);
      const node = graph.nodes.find((n) => n.id === id);
      if (node?.data.kind !== 'comment') throw new Error(`Unknown Comment reference: ${ref}`);
      const text = requireString(op.text, 'text', 4000);
      node.data = { ...node.data, text };
      changes.push(`Edit Comment (${ref})`);
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
    } else if (op.op === 'connect_action') {
      const fromRef = requireString(op.source_ref, 'source_ref', 80), toRef = requireString(op.target_ref, 'target_ref', 80);
      const source = refs.get(fromRef), target = refs.get(toRef);
      const sourceNode = graph.nodes.find((n) => n.id === source), targetNode = graph.nodes.find((n) => n.id === target);
      if (sourceNode?.data.kind !== 'application-action' || targetNode?.data.kind !== 'tool') throw new Error('Action wiring requires an application question and a tool target');
      if (graph.edges.some((e) => e.target === target && e.targetHandle === '__action__')) throw new Error('Tool already has an Action connection');
      const id = edgeId(source!, 'Action', target!, '__action__');
      if (graph.edges.some((e) => e.id === id)) throw new Error('Action connection already exists');
      graph.edges.push({ id, source: source!, sourceHandle: 'Action', target: target!, targetHandle: '__action__',
        data: { kind: 'application-action' }, style: { stroke: '#805ad5', strokeDasharray: '5 4' }, label: 'Action' });
      changes.push(`Connect ${fromRef}.Action → ${toRef}.Action`);
    } else if (op.op === 'connect_condition') {
      const fromRef = requireString(op.source_ref, 'source_ref', 80), toRef = requireString(op.target_ref, 'target_ref', 80);
      const source = refs.get(fromRef), target = refs.get(toRef);
      const sourceNode = graph.nodes.find((n) => n.id === source), targetNode = graph.nodes.find((n) => n.id === target);
      const anchor = requireString(op.source_anchor, 'source_anchor', 40);
      if (sourceNode?.data.kind !== 'application-condition' || !['application-action', 'application-error'].includes(targetNode?.data.kind ?? '') ||
          !['True Condition', 'False Condition'].includes(anchor)) throw new Error('Condition wiring requires a Condition branch and an application Action');
      if (graph.edges.some((e) => e.target === target && e.data?.kind === 'application-condition')) throw new Error('Target already has a Condition route');
      const id = edgeId(source!, anchor, target!, 'Condition');
      if (graph.edges.some((e) => e.id === id)) throw new Error('Condition route already exists');
      graph.edges.push({ id, source: source!, sourceHandle: anchor, target: target!, targetHandle: 'Condition',
        data: { kind: 'application-condition' }, style: { stroke: '#dd8a36', strokeDasharray: '4 4' }, label: anchor === 'True Condition' ? 'True' : 'False' });
      changes.push(`Route ${fromRef}.${anchor} → ${toRef}.Condition`);
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
  for (const edge of graph.edges.filter((e) => e.data?.kind === 'application-action')) {
    const action = graph.nodes.find((n) => n.id === edge.source), target = graph.nodes.find((n) => n.id === edge.target);
    if (action?.data.kind !== 'application-action' || target?.data.kind !== 'tool') throw new Error('Action connections must target installed workflow tools');
    if (!hasConfigPath(target.data.config, action.data.config_path))
      throw new Error(`Action ${action.id} targets a configuration path that does not exist on ${target.id}`);
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
    add_question: ['op', 'ref', 'question', 'config_path', 'position'],
    add_condition: ['op', 'ref', 'when', 'position'],
    add_error_message: ['op', 'ref', 'message', 'position'],
    add_comment: ['op', 'ref', 'text', 'position'],
    group: ['op', 'ref', 'node_refs', 'caption'],
    configure_container: ['op', 'node_ref', 'caption', 'disabled', 'collapsed'],
    ungroup_container: ['op', 'node_ref'],
    configure_question: ['op', 'node_ref', 'question', 'config_path'],
    configure_condition: ['op', 'node_ref', 'when'],
    configure_error_message: ['op', 'node_ref', 'message'],
    configure_comment: ['op', 'node_ref', 'text'],
    connect: ['op', 'source_ref', 'source_anchor', 'target_ref', 'target_anchor'],
    connect_action: ['op', 'source_ref', 'target_ref'],
    connect_condition: ['op', 'source_ref', 'source_anchor', 'target_ref'],
    disconnect: ['op', 'source_ref', 'source_anchor', 'target_ref', 'target_anchor'], remove: ['op', 'node_ref'],
  };
  const allowed = fields[op.op];
  if (!allowed || Object.keys(op).length !== allowed.length || Object.keys(op).some((key) => !allowed.includes(key))) throw new Error('Unsupported patch operation or unexpected operation fields');
  return op;
}
function requireRef(value: unknown, refs: Map<string, string>): string {
  const ref = requireString(value, 'ref', 80);
  if (!/^[a-zA-Z][a-zA-Z0-9_-]*$/.test(ref) || refs.has(ref)) throw new Error(`Invalid or duplicate new node ref: ${ref}`);
  return ref;
}
function validateQuestion(value: unknown): ApplicationActionNodeData['question'] {
  const q = requireObject(value, 'question');
  const allowed = ['id', 'prompt', 'description', 'type', 'control_field', 'required', 'default', 'options', 'secret', 'minimum', 'maximum', 'mode', 'extensions'];
  if (Object.keys(q).some((k) => !allowed.includes(k))) throw new Error('Application question contains unsupported fields');
  const id = requireString(q.id, 'question.id', 80), prompt = requireString(q.prompt, 'question.prompt', 300);
  const types = ['text', 'text-area', 'number', 'boolean', 'choice', 'multi-choice', 'date', 'file', 'folder', 'control-parameter', 'tree'];
  if (typeof q.type !== 'string' || !types.includes(q.type)) throw new Error('Unsupported application question type');
  if (q.type === 'control-parameter' && (typeof q.control_field !== 'string' || !q.control_field.trim())) throw new Error('Control parameters require a control_field mapping');
  if (q.secret && q.default !== undefined) throw new Error('Secret questions cannot contain a default value');
  if ((q.type === 'choice' || q.type === 'multi-choice') && (!Array.isArray(q.options) || q.options.length === 0 || q.options.some((x) => typeof x !== 'string') || new Set(q.options).size !== q.options.length)) throw new Error('Choice questions require unique string options');
  if (q.type === 'tree') {
    if (!Array.isArray(q.options) || q.options.length === 0 || q.options.length > 1000 || q.options.some((x) => typeof x !== 'string')) throw new Error('Tree questions require 1 to 1000 string paths');
    const normalized = (q.options as string[]).map((path) => path.split('>').map((part) => part.trim()));
    if (normalized.some((parts, index) => !parts.length || parts.length > 16 || parts.some((part) => !part) || (q.options as string[])[index] !== parts.join(' > ')) ||
        new Set(normalized.map((parts) => parts.join(' > '))).size !== normalized.length ||
        normalized.some((parts, i) => normalized.some((other, j) => i !== j && other.length > parts.length && parts.every((part, index) => other[index] === part))))
      throw new Error('Tree paths must be unique leaves with non-empty levels separated by >');
    if (q.default !== undefined && (typeof q.default !== 'string' || !(normalized.some((parts) => parts.join(' > ') === q.default))))
      throw new Error('Tree question default must match one configured path');
  }
  if ((q.mode !== undefined || q.extensions !== undefined) && q.type !== 'file') throw new Error('Only File Browse questions may set mode or extensions');
  if (q.mode !== undefined && q.mode !== 'open' && q.mode !== 'save') throw new Error('File Browse mode must be open or save');
  if (q.extensions !== undefined && (!Array.isArray(q.extensions) || q.extensions.length === 0 || q.extensions.length > 32 ||
      q.extensions.some((x) => typeof x !== 'string' || !/^[a-z0-9][a-z0-9_+-]{0,15}$/.test(x)) || new Set(q.extensions).size !== q.extensions.length))
    throw new Error('File Browse extensions must be unique lowercase names without dots');
  if ((q.type === 'file' || q.type === 'folder') && q.default !== undefined && typeof q.default !== 'string') throw new Error('Browse question default must be a path string');
  if (q.type === 'number' && ((q.minimum !== undefined && !Number.isFinite(q.minimum)) || (q.maximum !== undefined && !Number.isFinite(q.maximum)))) throw new Error('Numeric question bounds must be finite');
  if (q.type === 'number' && q.minimum !== undefined && q.maximum !== undefined && Number(q.minimum) > Number(q.maximum)) throw new Error('Question minimum exceeds maximum');
  if (q.type === 'number' && q.default !== undefined && (typeof q.default !== 'number' || !Number.isFinite(q.default))) throw new Error('Numeric question default must be finite');
  if (q.type === 'boolean' && q.default !== undefined && typeof q.default !== 'boolean') throw new Error('Boolean question default must be boolean');
  if (typeof q.required === 'boolean' || q.required === undefined) { /* valid */ } else throw new Error('Question required must be boolean');
  return { ...structuredClone(q), id, prompt } as unknown as ApplicationActionNodeData['question'];
}
function validateConfigPath(value: unknown): string[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > 32 || value.some((part) => typeof part !== 'string' || !part || part.length > 160 || ['__proto__', 'prototype', 'constructor'].includes(part)))
    throw new Error('config_path must be a non-empty list of safe configuration keys');
  return [...value] as string[];
}
function validatePredicate(value: unknown): ApplicationPredicate {
  const predicate = requireObject(value, 'condition');
  const valueOperators = ['equals', 'not_equals', 'contains', 'not_contains', 'greater_than', 'greater_than_or_equal', 'less_than', 'less_than_or_equal'];
  const allowed = [...valueOperators, 'truthy', 'falsy'];
  if (Object.keys(predicate).some((k) => !['question', 'operator', 'value'].includes(k)) ||
      typeof predicate.question !== 'string' || !predicate.question || typeof predicate.operator !== 'string' || !allowed.includes(predicate.operator) ||
      (valueOperators.includes(predicate.operator) !== ('value' in predicate)))
    throw new Error('Condition requires a question id, supported operator and matching value');
  return structuredClone(predicate) as unknown as ApplicationPredicate;
}
function hasConfigPath(config: Record<string, unknown>, path: string[]): boolean {
  let current: unknown = config;
  for (const part of path) {
    if (Array.isArray(current) && /^\d+$/.test(part)) current = current[Number(part)];
    else if (current && typeof current === 'object' && Object.prototype.hasOwnProperty.call(current, part)) current = (current as Record<string, unknown>)[part];
    else return false;
  }
  return current !== undefined;
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
