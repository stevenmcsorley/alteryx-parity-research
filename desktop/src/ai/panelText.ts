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
    default: return 'Working with Flowbench…';
  }
}

export type Usage = { input: number; output: number };
export const NO_USAGE: Usage = { input: 0, output: 0 };

export function addUsage(total: Usage, usage: { input_tokens?: number | null; output_tokens?: number | null } | undefined): Usage {
  return { input: total.input + (usage?.input_tokens ?? 0), output: total.output + (usage?.output_tokens ?? 0) };
}

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
