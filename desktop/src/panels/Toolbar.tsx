import { useState } from 'react';
import { useEditor } from '../state/store';
import { cancelRun, exportYxmd, openWorkflow, runWorkflow, saveWorkflow, describe } from '../state/engineActions';
import { promptPath } from '../ui/dialogs';
import { notify } from '../ui/toast';
import { DatabaseConnections } from './DatabaseConnections';
import { ServiceConnections } from './ServiceConnections';
import { Integrations } from './Integrations';

export async function doSave(forceAs = false): Promise<'saved' | 'cancelled' | 'failed'> {
  const s = useEditor.getState();
  let path = s.filePath;
  if (!path || forceAs) {
    path = await promptPath('Save workflow', path ?? `${s.baseDir}${s.baseDir.includes('\\') ? '\\' : '/'}${s.name.replace(/[^\w-]+/g, '_')}.flow.json`, 'flow.json');
    if (!path) return 'cancelled';
  }
  try {
    await saveWorkflow(path);
    notify(`Saved ${useEditor.getState().filePath}`);
    return 'saved';
  } catch (e) {
    notify(`Save failed: ${describe(e)}`);
    return 'failed';
  }
}

export async function doOpen() {
  const s = useEditor.getState();
  if (s.dirty && !window.confirm('Discard unsaved changes?')) return;
  const path = await promptPath('Open workflow (.flow.json, or import .yxmd/.yxmc/.yxwz/.yxzp)', s.filePath ?? s.baseDir, 'flow.json', 'open');
  if (!path) return;
  try {
    await openWorkflow(path);
  } catch (e) {
    notify(`Open failed: ${describe(e)}`);
  }
}

export async function doExport() {
  const s = useEditor.getState();
  const path = await promptPath('Export as .yxmd', `${s.baseDir}${s.baseDir.includes('\\') ? '\\' : '/'}${s.name.replace(/[^\w-]+/g, '_')}.yxmd`, 'yxmd');
  if (!path) return;
  try {
    const r = await exportYxmd(path);
    notify(r.written ? `Exported ${path}` : `Not exported: ${r.problems.length} node(s) have no export mapping (${r.problems[0]})`);
  } catch (e) {
    notify(`Export failed: ${describe(e)}`);
  }
}

export function doNew() {
  const s = useEditor.getState();
  if (s.dirty && !window.confirm('Discard unsaved changes?')) return;
  s.newWorkflow();
}

export function Toolbar({ onToggleAi, aiOpen }: { onToggleAi: () => void; aiOpen: boolean }) {
  const [dbOpen, setDbOpen] = useState(false);
  const [servicesOpen, setServicesOpen] = useState(false);
  const [integrationsOpen, setIntegrationsOpen] = useState(false);
  const name = useEditor((s) => s.name);
  const dirty = useEditor((s) => s.dirty);
  const canUndo = useEditor((s) => s.past.length > 0);
  const canRedo = useEditor((s) => s.future.length > 0);
  const status = useEditor((s) => s.run.status);
  const problems = useEditor((s) => s.problems.length);
  const validating = useEditor((s) => s.validating);
  const info = useEditor((s) => s.engineInfo);
  const engineError = useEditor((s) => s.engineError);
  const hasTools = useEditor((s) => s.nodes.some((n) => n.data.kind === 'tool'));
  const running = status === 'running';
  return (
    <header className="toolbar" role="toolbar" aria-label="Main toolbar">
      <div className="brand">
        <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden><rect x="1" y="3" width="7" height="6" rx="2" fill="var(--accent)" /><rect x="12" y="11" width="7" height="6" rx="2" fill="var(--ok)" /><path d="M8 6 C12 6 9 14 12 14" stroke="var(--text-2)" strokeWidth="1.6" fill="none" /></svg>
        Flowbench
      </div>
      <button onClick={doNew} title="New (Ctrl+N)">New</button>
      <button onClick={doOpen} title="Open (Ctrl+O)">Open</button>
      <button onClick={() => doSave()} title="Save (Ctrl+S)" data-testid="save">Save</button>
      <button onClick={() => doExport()} title="Export supported tools as .yxmd">Export</button>
      <button onClick={() => setDbOpen(true)} title="Manage PostgreSQL and SQL Server connections">Databases</button>
      <button onClick={() => setServicesOpen(true)} title="Manage Web/API and GitHub credentials">Web &amp; API</button>
      <button onClick={() => setIntegrationsOpen(true)} title="Connections, licences, providers, data packs and tool availability">Integrations</button>
      <button aria-pressed={aiOpen} onClick={onToggleAi} title="Open the Flowbench AI workspace">Flowbench AI</button>
      <span className="sep" />
      <button className="icon" onClick={() => useEditor.getState().undo()} disabled={!canUndo} title="Undo (Ctrl+Z)" aria-label="Undo">↶</button>
      <button className="icon" onClick={() => useEditor.getState().redo()} disabled={!canRedo} title="Redo (Ctrl+Y)" aria-label="Redo">↷</button>
      <button onClick={() => useEditor.getState().addComment({ x: 40, y: 40 })} title="Add comment">Comment</button>
      <button onClick={() => useEditor.getState().groupSelected()} title="Group selection in a container (Ctrl+G)">Group</button>
      <span className="sep" />
      <button className="primary" onClick={() => runWorkflow()} disabled={running || !info || !hasTools} title="Run (F5)" data-testid="run">
        {status === 'idle' || running ? 'Run' : 'Rerun'}
      </button>
      <button onClick={() => cancelRun()} disabled={!running} title="Cancel run (Esc)" data-testid="cancel">Cancel</button>
      <div className="title">
        <input type="text" value={name} aria-label="Workflow name" onChange={(e) => useEditor.setState({ name: e.target.value, dirty: true })} />
        {dirty && <span title="Unsaved changes">●</span>}
      </div>
      {engineError ? <span className="status-pill err" title={engineError}>engine offline</span>
        : validating ? <span className="status-pill muted">validating…</span>
          : problems ? <span className="status-pill err" data-testid="problem-count" onClick={() => useEditor.getState().setBottomTab('messages')} style={{ cursor: 'pointer' }}>{problems} problem{problems > 1 ? 's' : ''}</span>
            : hasTools ? <span className="status-pill ok" data-testid="valid">valid</span> : null}
      {running && <span className="status-pill run">running</span>}
      {info && <span style={{ color: 'var(--text-3)', fontSize: 11 }} title={`Python ${info.python}`}>engine {info.version}</span>}
      {dbOpen && <DatabaseConnections close={() => setDbOpen(false)} />}
      {servicesOpen && <ServiceConnections close={() => setServicesOpen(false)} />}
      {integrationsOpen && <Integrations close={() => setIntegrationsOpen(false)} />}
    </header>
  );
}
