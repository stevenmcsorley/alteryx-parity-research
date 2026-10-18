// Plain-language text for the AI panel: live activity steps, token counts and response problems.
import type { EditorState } from '../state/store';

type Args = Record<string, unknown>;

/** "node_3" -> "Summarize (node 3)", using the tool or caption the user sees on the canvas. */
export function nodeLabel(state: Pick<EditorState, 'nodes' | 'tools'>, ref: unknown): string {
  const index = Number(String(ref ?? '').match(/^node_(\d+)$/)?.[1] ?? 0) - 1;
  const node = state.nodes[index];
  if (!node) return String(ref ?? 'a node');
  const data = node.data as { kind?: string; tool?: string; label?: string; caption?: string };
  const name = data.caption || data.label || (data.tool && state.tools[data.tool]?.display_name) || data.tool || data.kind || 'node';
  return `${name} (node ${index + 1})`;
}

function patchSize(args: Args): number {
  try { return (JSON.parse(String(args.patch_json)).operations ?? []).length; } catch { return 0; }
}

export function activityLabel(tool: string, args: Args = {}, state?: Pick<EditorState, 'nodes' | 'tools'>): string {
  const node = (ref: unknown) => state ? nodeLabel(state, ref) : String(ref ?? '');
  switch (tool) {
    case 'workflow.propose_patch': {
      const n = patchSize(args);
      return n ? `Preparing ${n} workflow change${n === 1 ? '' : 's'}…` : 'Preparing a validated workflow change…';
    }
    case 'workflow.request_run': return 'Running the workflow…';
    case 'workflow.request_save': return 'Saving the workflow…';
    case 'run.get_results': return 'Reading the run results…';
    case 'run.get_result_page': {
      const offset = Number(args.offset) || 0, limit = Number(args.limit) || 0;
      const columns = String(args.columns ?? '').split(',').filter((c) => c.trim()).length;
      return `Reading rows ${offset + 1}–${offset + limit} of ${node(args.node_ref)}${columns ? `, ${columns} column${columns === 1 ? '' : 's'}` : ''}…`;
    }
    case 'workflow.get_schema': return `Checking the fields from ${node(args.node_ref)}…`;
    case 'workflow.get_node_config': return `Reading the settings of ${node(args.node_ref)}…`;
    case 'workflow.get_snapshot': case 'app.get_active_workflow': return 'Looking at the workflow…';
    case 'catalog.search_tools': return `Searching tools for “${String(args.query ?? '')}”…`;
    case 'catalog.get_tool_manifest': return `Reading how ${String(args.tool_id ?? 'a tool')} is configured…`;
    case 'app.get_capabilities': return 'Listing available tools…';
    case 'workflow.validate': case 'workflow.get_diagnostics': return 'Checking the workflow for problems…';
    case 'app.get_status': return 'Checking Flowbench status…';
    case 'agent.find_tools': return `Searching tools for “${String(args.query ?? '')}”…`;
    case 'agent.describe_tool': return `Reading the ${String(args.tool_id ?? 'tool').replace(/^core\./, '').replaceAll('_', ' ')} spec…`;
    case 'agent.formula_reference': return 'Reading the formula reference…';
    case 'agent.list_files': return `Listing files in ${String(args.path ?? 'the workspace')}…`;
    case 'agent.profile_file': return `Profiling ${String(args.path ?? 'a file')}…`;
    case 'agent.inspect_spreadsheet': return `Inspecting the sheets of ${String(args.path ?? 'a spreadsheet')}…`;
    case 'agent.read_text_file': return `Reading ${String(args.path ?? 'a file')}…`;
    case 'agent.write_text_file': return `Writing ${String(args.path ?? 'a file')}…`;
    case 'agent.make_folder': return `Creating ${String(args.path ?? 'a folder')}…`;
    case 'agent.extract_archive': return `Extracting ${String(args.path ?? 'an archive')}…`;
    case 'agent.download_file': return `Downloading ${String(args.url ?? 'a file')}…`;
    case 'agent.web_search': return `Searching the web for “${String(args.query ?? '')}”…`;
    case 'agent.search_datasets': return `Searching dataset catalogues for “${String(args.query ?? '')}”…`;
    case 'agent.fetch_page': return `Reading ${String(args.url ?? 'a web page')}…`;
    case 'agent.find_data_files': return `Looking for data files from ${String(args.url ?? 'a page')}…`;
    case 'agent.inspect_api': return `Inspecting the API at ${String(args.url ?? '')}…`;
    case 'agent.fetch_api': return `Fetching ${String(args.url ?? 'API data')}…`;
    case 'agent.query_results': return `Querying results of ${String(args.node_id ?? 'a node')}…`;
    case 'agent.run_evidence': return 'Collecting run evidence…';
    case 'agent.run_python': return 'Running a Python script…';
    case 'agent.run_r': return 'Running an R script…';
    case 'agent.run_command': return `Running ${((args.argv as string[] | undefined) ?? []).join(' ')}…`;
    case 'agent.docker': return `Running docker ${((args.args as string[] | undefined) ?? []).join(' ')}…`;
    case 'agent.install_python_packages': return `Installing ${((args.packages as string[] | undefined) ?? []).join(', ')}…`;
    case 'agent.inspect_environment': return 'Checking the installed software…';
    case 'agent.start_process': return `Starting ${((args.argv as string[] | undefined) ?? []).join(' ')}…`;
    case 'agent.process_status': return 'Checking a running process…';
    case 'agent.stop_process': return 'Stopping a process…';
    case 'agent.list_connections': return 'Listing saved connections…';
    case 'agent.list_providers': return 'Checking providers and integrations…';
    case 'agent.list_tables': return 'Listing database tables…';
    case 'agent.describe_table': return `Reading the columns of ${String(args.table ?? 'a table')}…`;
    case 'agent.query_database': return 'Querying the database…';
    case 'task.plan': return 'Planning the deliverables…';
    case 'task.deliver': return `Checking deliverable ${String(args.id ?? '')}…`;
    case 'task.classify': return 'Classifying the request…';
    case 'results.read': return `Reading part of a stored result${args.path ? ` (${String(args.path)})` : ''}…`;
    default: return 'Working with Flowbench…';
  }
}

/** Token use split by what it costs and why.
 *  fresh: input that is new this step (the prompt, new tool results); an estimate from the growth since the
 *         previous step of the same turn.
 *  resent: earlier context sent again and billed at the full rate.
 *  cached: input the provider served from its prompt cache (the repeated instructions and tool definitions),
 *          billed at a reduced rate.
 *  output: tokens the model generated, of which reasoning is the part spent thinking. */
export type Usage = { input: number; output: number; cached: number; fresh: number; resent: number; reasoning: number;
  steps: number; lastInput: number; lastOutput: number };
export const NO_USAGE: Usage = { input: 0, output: 0, cached: 0, fresh: 0, resent: 0, reasoning: 0, steps: 0, lastInput: 0, lastOutput: 0 };
export type StepUsage = { input_tokens?: number | null; output_tokens?: number | null; cached_tokens?: number | null; reasoning_tokens?: number | null };

/** Adds one model step to a turn's running total; returns the new total and this step's own breakdown. */
export function addStep(turn: Usage, usage: StepUsage | undefined): { total: Usage; step: Usage } {
  const input = usage?.input_tokens ?? 0;
  const output = usage?.output_tokens ?? 0;
  const cached = Math.min(usage?.cached_tokens ?? 0, input);
  const uncached = input - cached;
  const carried = turn.steps ? turn.lastInput + turn.lastOutput : 0;
  const fresh = Math.max(0, Math.min(uncached, input - carried));
  const step: Usage = { input, output, cached, fresh, resent: uncached - fresh, reasoning: Math.min(usage?.reasoning_tokens ?? 0, output),
    steps: 1, lastInput: input, lastOutput: output };
  return { total: { ...sumUsage(turn, step), lastInput: input, lastOutput: output }, step };
}

export function sumUsage(a: Usage, b: Usage): Usage {
  return { input: a.input + b.input, output: a.output + b.output, cached: a.cached + b.cached, fresh: a.fresh + b.fresh,
    resent: a.resent + b.resent, reasoning: a.reasoning + b.reasoning, steps: a.steps + b.steps, lastInput: b.lastInput, lastOutput: b.lastOutput };
}

/** Kept for callers that only need totals. */
export function addUsage(total: Usage, usage: StepUsage | undefined): Usage {
  return addStep(total, usage).total;
}

export const USAGE_PARTS = [
  { key: 'fresh', label: 'New input', note: 'Your message and new tool results (estimated)' },
  { key: 'resent', label: 'Re-sent context', note: 'Earlier conversation sent again at the full rate' },
  { key: 'cached', label: 'Cached', note: 'Repeated instructions and tool definitions, billed at a reduced rate' },
  { key: 'output', label: 'Generated', note: 'Written by the model, including reasoning' },
] as const;

export function tokens(n: number): string {
  if (n < 1000) return `${n}`;
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

type Diagnostic = { terminal_status?: string; error_code?: string | null; model?: string; provider?: string;
  usage?: { input_tokens?: number | null; output_tokens?: number | null } };

/** One line per response, for people rather than for bug reports. */
export function diagnosticSummary(entry: unknown): { text: string; problem: boolean } {
  const d = (entry ?? {}) as Diagnostic;
  const code = d.error_code ?? '';
  const used = `${tokens(d.usage?.input_tokens ?? 0)} in · ${tokens(d.usage?.output_tokens ?? 0)} out`;
  const problem = (text: string) => ({ text, problem: true });
  if (/usage|rate_limit|quota|http_429/.test(code)) return problem('Stopped: the plan’s usage limit was reached. Wait for it to reset, or switch to a local model.');
  if (/context_length/.test(code)) return problem('Stopped: the conversation is too long for the model. Start a new conversation.');
  if (/network|interrupt/.test(code)) return problem('Stopped: the network connection dropped. Retry to continue.');
  if (code === 'local_model_error') return problem('Stopped: the local model server returned an error. Check it is running and the model supports tools.');
  if (code === 'provider_stream_error' || code === 'provider_response_failed') return problem('Stopped: the provider ended the response with an error. If this repeats, the usage limit may have been reached.');
  if (/^http_/.test(code)) return problem(`Stopped: the provider returned ${code.replace('http_', 'HTTP ')}.`);
  if (code) return problem(`Stopped: ${code.replaceAll('_', ' ')}.`);
  if (d.terminal_status === 'cancelled') return { text: 'Stopped by you.', problem: false };
  if (d.terminal_status && d.terminal_status !== 'completed') return problem(`Ended early (${d.terminal_status.replaceAll('_', ' ')}).`);
  return { text: `Completed · ${used}`, problem: false };
}
