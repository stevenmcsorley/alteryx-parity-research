import type { EditorState } from '../state/store';

/** Stable, read-only first slice of the Flowbench agent protocol. */
export const AGENT_PROTOCOL = 'flowbench.agent.v1' as const;

export type AgentToolName =
  | 'app.get_status'
  | 'app.get_capabilities'
  | 'app.get_active_workflow'
  | 'catalog.search_tools'
  | 'catalog.get_tool_manifest'
  | 'workflow.get_snapshot'
  | 'workflow.validate'
  | 'workflow.get_diagnostics';

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
  { name: 'catalog.search_tools', description: 'Search the catalogue by tool name, category, or implementation status.', parameters: objectSchema({ query: { type: 'string' } }, ['query']) },
  { name: 'catalog.get_tool_manifest', description: 'Read an installed tool manifest by tool ID.', parameters: objectSchema({ tool_id: { type: 'string' } }, ['tool_id']) },
  { name: 'workflow.get_snapshot', description: 'Read a bounded, sanitised snapshot of the active workflow. No row data or secrets are included.', parameters: objectSchema() },
  { name: 'workflow.validate', description: 'Run the existing Flowbench workflow validator and return its current diagnostics.', parameters: objectSchema() },
  { name: 'workflow.get_diagnostics', description: 'Read existing workflow validation problems and latest run messages.', parameters: objectSchema() },
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
};

/** Dispatcher deliberately contains no mutation, filesystem, credential or execution tools. */
export async function dispatchAgentTool(
  environment: AgentToolEnvironment,
  name: string,
  args: unknown = {},
): Promise<unknown> {
  const input = validateArguments(name, args);
  const state = environment.getState();
  switch (name as AgentToolName) {
    case 'app.get_status':
      return { protocol: AGENT_PROTOCOL, engine: state.engineInfo ? { connected: true, version: state.engineInfo.version } : { connected: false, error: state.engineError }, run: { status: state.run.status, run_id: state.run.runId } };
    case 'app.get_capabilities':
      return { protocol: AGENT_PROTOCOL, catalogue_entries: state.catalog.length, installed_kernels: Object.keys(state.tools).length,
        implemented_catalogue_entries: state.catalog.filter((entry) => entry.implementation_status === 'implemented').length,
        tools: state.catalog.map(({ id, name: label, category, implementation_status, verification_status, kernel }) =>
          ({ id, name: label, category, implementation_status, verification_status, kernel })) };
    case 'app.get_active_workflow':
      return { name: state.name, description: state.description ?? null, node_count: state.nodes.length, edge_count: state.edges.length,
        profile: state.profile };
    case 'catalog.search_tools': {
      const needle = String(input.query).trim().toLocaleLowerCase();
      if (!needle) return [];
      return state.catalog.filter((entry) => `${entry.id} ${entry.name} ${entry.category} ${entry.implementation_status}`.toLocaleLowerCase().includes(needle))
        .slice(0, 50).map(({ id, name: label, category, implementation_status, verification_status, kernel, unavailable_reason }) =>
          ({ id, name: label, category, implementation_status, verification_status, kernel, unavailable_reason }));
    }
    case 'catalog.get_tool_manifest': {
      const tool = state.tools[String(input.tool_id)];
      if (!tool) throw new Error(`No installed tool manifest for ${String(input.tool_id)}`);
      return structuredClone(tool);
    }
    case 'workflow.get_snapshot':
      return snapshot(state);
    case 'workflow.validate':
      await environment.validate();
      return { problems: environment.getState().problems };
    case 'workflow.get_diagnostics':
      return { validating: state.validating, problems: state.problems.map(sanitiseDiagnostic), latest_run: {
        status: state.run.status,
        events: state.run.events.slice(-100).map(({ ts, type, node_id, message, code, duration_ms, output_rows, input_rows }) =>
          ({ ts, type, node_id, message: message ? redactText(message) : undefined, code, duration_ms, output_rows, input_rows })),
      } };
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
    if (typeof item !== definition.parameters.properties[key]?.type) throw new Error(`Argument ${key} must be a ${definition.parameters.properties[key]?.type}`);
  }
  return args;
}

function snapshot(state: AgentContext) {
  return {
    protocol: AGENT_PROTOCOL,
    name: state.name,
    description: state.description ?? null,
    profile: state.profile,
    nodes: state.nodes.map((node) => ({ id: node.id, kind: node.data.kind,
      ...(node.data.kind === 'tool' ? { tool: node.data.tool, configured_options: Object.keys(node.data.config ?? {}) } : {}) })),
    edges: state.edges.map(({ source, sourceHandle, target, targetHandle, data }) => ({ source, source_anchor: sourceHandle,
      target, target_anchor: targetHandle, ...(data?.kind ? { kind: data.kind } : {}) })),
    schemas: state.schemas,
    problems: state.problems.map(sanitiseDiagnostic),
    privacy: 'No option values, result rows, credential material, connection passwords, raw file contents, or queries are included.',
  };
}

function redactText(value: string): string {
  return value.replace(/(password|passwd|token|secret|connection\s*string)\s*[:=]\s*[^\s,;]+/ig, '$1=[REDACTED]');
}
function sanitiseDiagnostic(problem: { code: string; message: string; node_id: string | null }) {
  return { ...problem, message: redactText(problem.message) };
}
