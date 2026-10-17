import type { AgentTask, TaskPhase } from './conversation';

const PHASES: TaskPhase[] = ['discover', 'plan', 'acquire', 'build', 'validate', 'execute', 'inspect', 'repair', 'complete'];
const LABEL: Record<TaskPhase, string> = { discover: 'Discover', plan: 'Plan', acquire: 'Acquire', build: 'Build', validate: 'Validate',
  execute: 'Execute', inspect: 'Inspect', repair: 'Repair', complete: 'Complete' };
const TOOL_LABEL: Record<string, string> = {
  'agent.find_tools': 'Searched tools', 'agent.describe_tool': 'Read a tool spec', 'agent.list_files': 'Listed files',
  'agent.profile_file': 'Profiled a file', 'agent.inspect_spreadsheet': 'Inspected a spreadsheet', 'agent.download_file': 'Downloaded a file',
  'agent.query_results': 'Queried results', 'agent.run_evidence': 'Collected run evidence', 'workflow.propose_patch': 'Edited the workflow',
  'workflow.validate': 'Validated', 'workflow.request_run': 'Ran the workflow', 'workflow.request_save': 'Saved the workflow',
  'run.get_results': 'Read run results', 'run.get_result_page': 'Read result rows', 'workflow.get_snapshot': 'Read the workflow',
  'workflow.get_schema': 'Read output fields', 'workflow.get_node_config': 'Read node settings', 'workflow.get_diagnostics': 'Read diagnostics',
  'catalog.search_tools': 'Searched the catalogue', 'catalog.get_tool_manifest': 'Read a manifest', 'task.classify': 'Classified the request',
};

export const STATUS_TEXT: Record<string, string> = {
  running: 'Working', completed: 'Completed', unverified: 'Finished without a Flowbench run', failed: 'Stopped by an error', cancelled: 'Stopped',
};

function time(seconds: number) {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/** The task controller's view of a turn: phases, actions, runs, artifacts, errors and verification. */
export function TaskView({ task, compact = false }: { task: AgentTask; compact?: boolean }) {
  const visited = new Set(task.actions.map((a) => a.phase));
  if (task.status === 'completed') visited.add('complete');
  const last = task.actions[task.actions.length - 1];
  if (compact) return <div className="ai-task-strip" role="status" aria-label="Current task">
    <span className="ai-task-phase">{LABEL[task.phase]}</span>
    <span className="ai-task-objective" title={task.objective}>{task.objective}</span>
    {last && <span className="ai-task-last">{TOOL_LABEL[last.tool] ?? last.tool}{last.ok === false ? ' — failed' : ''}</span>}
  </div>;
  return <section className="ai-task" aria-label="Task details">
    <header>
      <strong title={task.objective}>{task.objective}</strong>
      <span className={`ai-task-status ${task.status}`}>{STATUS_TEXT[task.status] ?? task.status}</span>
    </header>
    <ol className="ai-task-phases" aria-label="Task phases">
      {PHASES.filter((p) => p !== 'repair' || visited.has('repair')).map((p) =>
        <li key={p} className={`${visited.has(p) ? 'visited' : ''} ${task.phase === p ? 'current' : ''}`}>{LABEL[p]}</li>)}
    </ol>
    {task.kind === 'question' && <p className="ai-menu-note">{task.classified_as_question
      ? `Answered without running Flowbench: ${task.classified_as_question}` : 'An ordinary question: no execution needed.'}</p>}
    {!!task.actions.length && <ul className="ai-task-actions" aria-label="Actions">
      {task.actions.map((a, i) => <li key={i} className={a.ok === false ? 'failed' : a.ok ? 'ok' : 'pending'}>
        <span className="ai-task-mark" aria-hidden="true">{a.ok === false ? '✗' : a.ok ? '✓' : '…'}</span>
        <span>{TOOL_LABEL[a.tool] ?? a.tool}</span>
        {a.summary && a.summary !== 'ok' && <span className="ai-task-summary">{a.summary}</span>}
        <time>{time(a.at)}</time>
      </li>)}
    </ul>}
    {!!task.runs.length && <div className="ai-task-group"><h4>Runs</h4>
      {task.runs.map((r, i) => <div key={i} className={r.status === 'succeeded' ? 'ok' : 'failed'}>{r.status} · {r.outputs} output{r.outputs === 1 ? '' : 's'}{r.run_id ? ` · ${r.run_id.slice(0, 8)}` : ''}</div>)}</div>}
    {!!task.artifacts.length && <div className="ai-task-group"><h4>Files</h4>
      {task.artifacts.map((a, i) => <div key={i} title={a.sha256 ? `SHA-256 ${a.sha256}` : undefined}>{a.path}{a.url ? <> · from <span className="ai-task-url">{a.url}</span></> : null}</div>)}</div>}
    {!!task.errors.length && <div className="ai-task-group"><h4>Errors and recovery</h4>
      {task.errors.map((e, i) => <div key={i} className="failed">{e.tool}: {typeof e.error === 'string' ? e.error : (e.error as { message?: string } | null)?.message ?? 'failed'}</div>)}</div>}
    <p className={`ai-task-verdict ${task.verification}`}>{{
      verified: `Backed by Flowbench execution (${task.evidence.length} piece${task.evidence.length === 1 ? '' : 's'} of evidence).`,
      unverified: 'No Flowbench run backs the answer.', answered_without_execution: 'Answered without Flowbench execution.',
      not_required: 'No execution was needed.' }[task.verification]}</p>
  </section>;
}
