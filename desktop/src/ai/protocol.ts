import type { EditorState } from '../state/store';
import type { CanvasNode } from '../model/workflow';
import { workflowRevision } from './workflowPatch';
import agentToolResults from '../../agent-tool-results.v1.json';

/** Versioned application protocol for Flowbench's installed tool catalogue and editor. */
export const AGENT_PROTOCOL = 'flowbench.agent.v1' as const;

export type AgentToolName =
  | 'app.get_status'
  | 'app.get_capabilities'
  | 'app.get_active_workflow'
  | 'catalog.search_tools'
  | 'catalog.get_tool_manifest'
  | 'workflow.get_snapshot'
  | 'workflow.get_node_config'
  | 'workflow.get_schema'
  | 'workflow.validate'
  | 'workflow.get_diagnostics'
  | 'workflow.propose_patch'
  | 'workflow.request_run'
  | 'workflow.request_save'
  | 'run.get_results'
  | 'run.get_result_page';

export type AgentToolDefinition = {
  name: AgentToolName;
  description: string;
  parameters: {
    type: 'object';
    properties: Record<string, { type: string; description?: string; maximum?: number }>;
    required: string[];
    additionalProperties: false;
  };
};

export const AGENT_TOOLS: AgentToolDefinition[] = [
  { name: 'app.get_status', description: 'Read the current engine and workflow run status.', parameters: objectSchema() },
  { name: 'app.get_capabilities', description: 'List implemented Flowbench catalogue capabilities and their evidence status.', parameters: objectSchema() },
  { name: 'app.get_active_workflow', description: 'Read the open workflow name and basic graph counts.', parameters: objectSchema() },
  { name: 'catalog.search_tools', description: 'Search the catalogue by tool name, category, kernel ID, or implementation status. The result maps catalogue IDs to executable kernel IDs where available.', parameters: objectSchema({ query: { type: 'string' } }, ['query']) },
  { name: 'catalog.get_tool_manifest', description: 'Read an installed tool manifest using its executable kernel ID (the kernel field from catalog.search_tools).', parameters: objectSchema({ tool_id: { type: 'string' } }, ['tool_id']) },
  { name: 'workflow.get_snapshot', description: 'Read the active workflow graph, connections, node names, and installed tool IDs. No row data or secrets are included.', parameters: objectSchema() },
  { name: 'workflow.get_schema', description: 'Read field names and effective types for an existing workflow node output. No records or configuration values are included.', parameters: objectSchema({ node_ref: { type: 'string' } }, ['node_ref']) },
  { name: 'workflow.validate', description: 'Run the existing Flowbench workflow validator and return its current diagnostics.', parameters: objectSchema() },
  { name: 'workflow.get_diagnostics', description: 'Read existing workflow validation problems and latest run messages.', parameters: objectSchema() },
  { name: 'workflow.get_node_config', description: 'Read the active workflow node configuration, including file locations and expressions. Credential fields and raw records are excluded.', parameters: objectSchema({ node_ref: { type: 'string' } }, ['node_ref']) },
  { name: 'workflow.propose_patch', description: 'Prepare and validate a workflow graph change. In Review mode it is previewed for approval; in Full Control mode it is applied as one validated, undoable editor transaction.', parameters: objectSchema({ patch_json: { type: 'string', description: 'JSON object {"operations":[...]}. Operations: add {op,ref,tool_id,config,position}; configure {op,node_ref,config}; add_question {op,ref,question,config_path,position}; add_condition {op,ref,when,position}; configure_question {op,node_ref,question,config_path}; configure_condition {op,node_ref,when}; connect {op,source_ref,source_anchor,target_ref,target_anchor}; connect_action {op,source_ref,target_ref}; connect_condition {op,source_ref,source_anchor,target_ref}, where source_anchor is exactly "True Condition" or "False Condition"; disconnect {op,source_ref,source_anchor,target_ref,target_anchor}; remove {op,node_ref}. Use installed manifests and node_N references from workflow.get_snapshot. Custom Condition snapshot aliases: output_1=True Condition, output_2=False Condition, input_1=Action Condition input; use connect_condition for those links.' }, base_revision: { type: 'string', description: 'Opaque revision returned by workflow.get_snapshot.' } }, ['patch_json', 'base_revision']) },
  { name: 'workflow.request_run', description: 'Run the current workflow. Review mode requests confirmation; Full Control may run ordinary local processing immediately. File writes, scripts, database writes and other external effects retain their scoped permission checks.', parameters: objectSchema() },
  { name: 'workflow.request_save', description: 'Save the active workflow. Full Control saves to the current authorized workflow path; choosing a new destination still uses the native file picker.', parameters: objectSchema() },
  { name: 'run.get_results', description: 'Read bounded previews from the most recent completed run. Results are shared only under the active data-routing consent.', parameters: objectSchema() },
  { name: 'run.get_result_page', description: 'Read actual rows from one Browse output in the latest completed run. Use node_ref values from run.get_results and request the row range needed for the analysis.', parameters: objectSchema({ node_ref: { type: 'string' }, offset: { type: 'integer' }, limit: { type: 'integer' } }, ['node_ref', 'offset', 'limit']) },
];

function objectSchema(properties: AgentToolDefinition['parameters']['properties'] = {}, required: string[] = []) {
  return { type: 'object' as const, properties, required, additionalProperties: false as const };
}

export type AgentContext = Pick<EditorState,
  'tools' | 'catalog' | 'engineInfo' | 'engineError' | 'nodes' | 'edges' | 'name' | 'description' |
  'profile' | 'problems' | 'schemas' | 'validating' | 'run' | 'history' | 'selectedResult'>;

export type AgentToolEnvironment = {
  getState: () => AgentContext;
  validate: () => Promise<void>;
  proposePatch?: (patchJson: string, baseRevision: string) => Promise<unknown>;
  requestRun?: () => unknown;
  requestSave?: () => unknown;
  getResults?: () => unknown;
  getResultPage?: (nodeRef: string, offset: number, limit: number) => unknown;
};

type ResultShape = { type: string; required?: Record<string, string>; optional?: Record<string, string>;
  additionalProperties?: boolean; maxItems?: number };
const resultContracts = agentToolResults as { protocol: string; version: number;
  common_error: ResultShape; contracts: Record<string, ResultShape> };

/** Check the renderer producer against the same versioned top-level shapes consumed by Rust. */
export function agentResultContractError(name: string, value: unknown): string | null {
  const commonError = shapeError(value, resultContracts.common_error, '$');
  if (commonError === null) return null;
  const contract = resultContracts.contracts[name];
  if (!contract) return `unregistered result contract for ${name}`;
  return shapeError(value, contract, '$');
}

/** Mutations/execution can only enter through explicit editor callbacks that stage approvals. */
export async function dispatchAgentTool(
  environment: AgentToolEnvironment,
  name: string,
  args: unknown = {},
): Promise<unknown> {
  const result = await dispatchAgentToolUnchecked(environment, name, args);
  const violation = agentResultContractError(name, result);
  if (violation) throw new Error(`Flowbench result contract violation for ${name} at ${violation}`);
  return result;
}

async function dispatchAgentToolUnchecked(
  environment: AgentToolEnvironment,
  name: string,
  args: unknown,
): Promise<unknown> {
  const input = validateArguments(name, args);
  const state = environment.getState();
  switch (name as AgentToolName) {
    case 'app.get_status':
      return { protocol: AGENT_PROTOCOL, engine: { connected: Boolean(state.engineInfo), version: state.engineInfo?.version ?? 'unavailable' }, run: { status: state.run.status } };
    case 'app.get_capabilities':
      return { protocol: AGENT_PROTOCOL, catalogue_entries: state.catalog.length, installed_kernels: Object.keys(state.tools).length,
        implemented_catalogue_entries: state.catalog.filter((entry) => entry.implementation_status === 'implemented').length,
        tools: state.catalog.map(({ id, kernel, implementation_status, verification_status }) =>
          ({ id, kernel, implementation_status, verification_status })) };
    case 'app.get_active_workflow':
      return { node_count: state.nodes.length, edge_count: state.edges.length };
    case 'catalog.search_tools': {
      const needle = String(input.query).trim().toLocaleLowerCase();
      if (!needle) return [];
      return state.catalog.filter((entry) => `${entry.id} ${entry.name} ${entry.category} ${entry.kernel ?? ''} ${entry.implementation_status}`.toLocaleLowerCase().includes(needle))
        .map(({ id, kernel, implementation_status, verification_status }) => ({ id, kernel, implementation_status, verification_status }));
    }
    case 'catalog.get_tool_manifest': {
      const tool = state.tools[String(input.tool_id)];
      if (!tool) throw new Error(`No installed tool manifest for ${String(input.tool_id)}`);
      return projectManifest(tool);
    }
    case 'workflow.get_snapshot':
      return snapshot(state);
    case 'workflow.get_node_config': {
      const index = Number(String(input.node_ref).match(/^node_(\d+)$/)?.[1] ?? 0) - 1;
      const node = state.nodes[index];
      if (!node) throw new Error('Unknown workflow node reference');
      if (node.data.kind === 'application-action') {
        const { default: answerDefault, secret, ...publicQuestion } = node.data.question;
        return { node_ref: `node_${index + 1}`, kind: 'application-action', config: {
          question: { ...publicQuestion, ...(secret || answerDefault === undefined ? {} : { default: answerDefault }) },
          config_path: [...node.data.config_path], condition: node.data.when ?? null,
        } };
      }
      if (node.data.kind === 'application-condition') return { node_ref: `node_${index + 1}`, kind: 'application-condition', config: { condition: node.data.when } };
      if (node.data.kind !== 'tool') return { node_ref: `node_${index + 1}`, kind: node.data.kind, config: {} };
      return { node_ref: `node_${index + 1}`, kind: 'tool', tool_id: node.data.tool, config: safeConfigProjection(node.data.config) };
    }
    case 'workflow.get_schema': {
      const index = Number(String(input.node_ref).match(/^node_(\d+)$/)?.[1] ?? 0) - 1;
      const node = state.nodes[index];
      if (!node) throw new Error('Unknown workflow node reference');
      const outputs = state.schemas[node.id] ?? {};
      return { node_ref: `node_${index + 1}`, anchors: Object.entries(outputs).slice(0, 20).map(([anchor, fields]) => ({
        anchor: anchorRef(anchor, node, state.tools, 'output') ?? 'output',
        anchor_name: anchor,
        fields: fields.slice(0, 100).filter((field) => !/password|passwd|secret|token|credential|api.?key|access.?key/i.test(field.name))
          .map(({ name, type, size, scale }) => ({ name, type, ...(Number.isFinite(size) ? { size } : {}), ...(Number.isFinite(scale) ? { scale } : {}) })),
      })) };
    }
    case 'workflow.validate':
      await environment.validate();
      return safeProblems(environment.getState());
    case 'workflow.get_diagnostics':
      return { validating: state.validating, problems: safeProblems(state), latest_run: {
        status: state.run.status,
        events: state.run.events.slice(-100).map(({ type, node_id, code, duration_ms }) =>
          ({ type, node_ref: nodeRef(state, node_id), code: safeCode(code), duration_ms })),
      } };
    case 'workflow.propose_patch':
      if (!environment.proposePatch) throw new Error('Workflow editing is unavailable in this application context');
      return environment.proposePatch(String(input.patch_json), String(input.base_revision));
    case 'workflow.request_run':
      if (!environment.requestRun) throw new Error('Workflow execution is unavailable in this application context');
      return environment.requestRun();
    case 'workflow.request_save':
      if (!environment.requestSave) throw new Error('Workflow saving is unavailable in this application context');
      return environment.requestSave();
    case 'run.get_results':
      if (!environment.getResults) throw new Error('Run results are unavailable in this application context');
      return environment.getResults();
    case 'run.get_result_page': {
      const offset = input.offset as number, limit = input.limit as number;
      if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) || limit < 1)
        throw new Error('Result page offset must be nonnegative and limit must be positive');
      if (!environment.getResultPage) throw new Error('Run result paging is unavailable in this application context');
      return environment.getResultPage(String(input.node_ref), offset, limit);
    }
    default:
      throw new Error(`Unknown or unavailable agent tool: ${name}`);
  }
}

function validateArguments(name: string, value: unknown): Record<string, unknown> {
  if (!AGENT_TOOLS.some((tool) => tool.name === name)) throw new Error(`Unknown or unavailable agent tool: ${name}`);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Tool arguments must be an object');
  const args = value as Record<string, unknown>;
  const definition = AGENT_TOOLS.find((tool) => tool.name === name)!;
  for (const key of Object.keys(args)) if (!(key in definition.parameters.properties)) throw new Error(`Unexpected argument: ${key}`);
  for (const key of definition.parameters.required) if (!(key in args)) throw new Error(`Missing argument: ${key}`);
  for (const [key, item] of Object.entries(args)) {
    const expected = definition.parameters.properties[key]?.type;
    if (expected === 'integer' ? typeof item !== 'number' || !Number.isSafeInteger(item) : typeof item !== expected)
      throw new Error(`Argument ${key} must be a ${expected}`);
  }
  return args;
}

function snapshot(state: AgentContext) {
  const refs = new Map(state.nodes.map((node, index) => [node.id, `node_${index + 1}`]));
  return {
    protocol: AGENT_PROTOCOL,
    base_revision: workflowRevision(state as EditorState),
    nodes: state.nodes.map((node, index) => {
      const manifest = node.data.kind === 'tool' ? state.tools[node.data.tool] : undefined;
      const kernel = node.data.kind === 'tool' ? node.data.tool : undefined;
      const catalogId = manifest?.catalog_ids.find((id) => state.catalog.some((entry) => entry.id === id))
        ?? (kernel ? state.catalog.find((entry) => entry.kernel === kernel)?.id
          : node.data.kind === 'application-condition' ? 'interface:condition' : node.data.kind === 'application-action' ? 'interface:action' : undefined);
      return { ref: `node_${index + 1}`, kind: node.data.kind,
        ...(catalogId ? { tool_id: catalogId } : node.data.kind === 'tool' ? { tool_available: false } : {}) };
    }),
    nodes_truncated: false,
    edges: state.edges.map(({ source, sourceHandle, target, targetHandle }) => ({ source_ref: refs.get(source),
      source_anchor: anchorRef(sourceHandle, state.nodes.find((node) => node.id === source), state.tools, 'output'),
      target_ref: refs.get(target), target_anchor: anchorRef(targetHandle, state.nodes.find((node) => node.id === target), state.tools, 'input') })),
    edges_truncated: false,
    problems: safeProblems(state),
    privacy: 'Workflow names/descriptions, node identifiers, configuration values, schemas, rows, file paths, query text, and diagnostic messages are excluded.',
  };
}
function anchorRef(handle: string | null | undefined, node: CanvasNode | undefined, tools: AgentContext['tools'], direction: 'input' | 'output') {
  if (!handle || !node) return null;
  if (node.data.kind === 'application-action') return direction === 'output' && handle === 'Action' ? 'output_1'
    : direction === 'input' && handle === 'Condition' ? 'input_1' : null;
  if (node.data.kind === 'application-condition') return direction === 'output' && handle === 'True Condition' ? 'output_1'
    : direction === 'output' && handle === 'False Condition' ? 'output_2' : null;
  if (node.data.kind !== 'tool') return null;
  const manifest = tools[node.data.tool];
  const anchors = direction === 'input' ? manifest?.inputs : manifest?.outputs;
  const index = anchors?.findIndex((anchor) => anchor.name === handle) ?? -1;
  return index < 0 ? null : `${direction}_${index + 1}`;
}

function safeProblems(state: AgentContext) {
  return { count: state.problems.length, problems: state.problems.map(({ code, node_id }) => ({ code: safeCode(code), node_ref: nodeRef(state, node_id) })) };
}

function safeConfigProjection(config: Record<string, unknown>, depth = 0): Record<string, unknown> {
  if (depth > 64) return {};
  const omitted = /pass(word|wd)?|secret|token|credential|api.?key|access.?key|authorization|connection|(^|_)(rows?|records?|raw_data)(_|$)/i;
  const output: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(config)) {
    if (omitted.test(key)) continue;
    if (typeof value === 'string') {
      if (sensitiveConfigText(value)) continue;
      output[key] = value;
    }
    else if (typeof value === 'number' || typeof value === 'boolean' || value === null) output[key] = value;
    else if (Array.isArray(value)) output[key] = value.map((item) =>
      item && typeof item === 'object' && !Array.isArray(item) ? safeConfigProjection(item as Record<string, unknown>, depth + 1)
        : item);
    else if (value && typeof value === 'object') output[key] = safeConfigProjection(value as Record<string, unknown>, depth + 1);
  }
  return output;
}
function sensitiveConfigText(value: string) {
  const lower = value.toLocaleLowerCase();
  return ['password=', 'passwd=', 'pwd=', 'access_token=', 'api_key=', 'secret=', 'authorization:', 'bearer '].some((part) => lower.includes(part))
    || (lower.includes('://') && lower.includes('@'));
}

function shapeError(value: unknown, shape: ResultShape, path: string): string | null {
  const type = shape.type;
  const matches = type === 'array' ? Array.isArray(value) : type === 'object' ? Boolean(value && typeof value === 'object' && !Array.isArray(value))
    : type === 'integer' ? typeof value === 'number' && Number.isSafeInteger(value)
      : type === 'null' ? value === null : typeof value === type;
  if (!matches) return `${path} expected ${type}`;
  if (type === 'array' && shape.maxItems !== undefined && (value as unknown[]).length > shape.maxItems) return `${path} exceeds maxItems`;
  if (type === 'object') {
    const object = value as Record<string, unknown>;
    for (const [key, expected] of Object.entries(shape.required ?? {})) {
      if (!(key in object)) return `${path}.${key} is required`;
      const violation = shapeError(object[key], { type: expected }, `${path}.${key}`);
      if (violation) return violation;
    }
    for (const [key, expected] of Object.entries(shape.optional ?? {})) {
      if (key in object) {
        const violation = shapeError(object[key], { type: expected }, `${path}.${key}`);
        if (violation) return violation;
      }
    }
    if (shape.additionalProperties === false) {
      const allowed = new Set([...Object.keys(shape.required ?? {}), ...Object.keys(shape.optional ?? {})]);
      const extra = Object.keys(object).find((key) => !allowed.has(key));
      if (extra) return `${path}.${extra} is not allowed`;
    }
  }
  return null;
}
function nodeRef(state: AgentContext, nodeId: string | null) {
  if (!nodeId) return null;
  const index = state.nodes.findIndex((node) => node.id === nodeId);
  return index < 0 ? null : `node_${index + 1}`;
}
function safeCode(code: string | undefined) {
  return code && /^[a-z0-9-]{1,64}$/i.test(code) ? code : 'unspecified';
}
function projectManifest(manifest: AgentContext['tools'][string]) {
  return {
    tool_id: manifest.tool_id,
    version: manifest.version,
    catalog_ids: manifest.catalog_ids,
    inputs: manifest.inputs.map(({ name, required, multiple }) => ({ name, required, multiple })),
    outputs: manifest.outputs.map(({ name }) => ({ name })),
    config_schema: projectSchema(manifest.config_schema),
  };
}
function projectSchema(schema: import('../engine/types').JsonSchema): unknown {
  return { ...(schema.type ? { type: schema.type } : {}),
    ...(schema.enum && schema.enum.every((value) => ['string', 'number', 'boolean'].includes(typeof value)) ? { enum: schema.enum } : {}),
    ...(Number.isFinite(schema.minimum) ? { minimum: schema.minimum } : {}),
    ...(Number.isFinite(schema.maximum) ? { maximum: schema.maximum } : {}),
    ...(Number.isFinite(schema.minLength) ? { minLength: schema.minLength } : {}),
    ...(Number.isFinite(schema.maxLength) ? { maxLength: schema.maxLength } : {}),
    ...(Number.isFinite(schema.minItems) ? { minItems: schema.minItems } : {}),
    ...(schema.required ? { required: schema.required } : {}),
    ...(schema.properties ? { properties: Object.fromEntries(Object.entries(schema.properties).map(([key, value]) => [key, projectSchema(value)])) } : {}),
    ...(schema.items ? { items: projectSchema(schema.items) } : {}),
    ...(schema.oneOf ? { oneOf: schema.oneOf.map(projectSchema) } : {}),
    ...(typeof schema.additionalProperties === 'boolean' ? { additionalProperties: schema.additionalProperties } : {}) };
}
