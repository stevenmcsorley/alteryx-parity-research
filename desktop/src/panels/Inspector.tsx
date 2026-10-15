import { useEffect, useMemo, useRef, useState } from 'react';
import { useEditor } from '../state/store';
import type { FieldDef } from '../engine/types';
import type { ApplicationActionNodeData, ApplicationConditionNodeData, ApplicationErrorNodeData, CanvasNode, ToolNodeData } from '../model/workflow';
import { SchemaForm, type FieldContext } from './SchemaForm';
import { categoryColor, glyph } from '../ui/categories';
import { engine, describe } from '../state/engineActions';

export function Inspector({ embedded = false }: { embedded?: boolean } = {}) {
  const selected = useEditor((s) => s.nodes.filter((n) => n.selected));
  if (selected.length > 1) return <MultiSelection count={selected.length} embedded={embedded} />;
  const node = selected[0];
  return (
    <aside className={embedded ? 'inspector-content' : 'app-right'} aria-label="Configuration" data-testid="inspector">
      {!node ? <WorkflowProperties /> : node.data.kind === 'tool' ? <ToolInspector node={node} /> :
        node.data.kind === 'application-action' ? <ApplicationActionInspector node={node} /> :
        node.data.kind === 'application-condition' ? <ApplicationConditionInspector node={node} /> :
        node.data.kind === 'application-error' ? <ApplicationErrorInspector node={node} /> :
        node.data.kind === 'container' ? <ContainerInspector node={node} /> : <CommentInspector node={node} />}
    </aside>
  );
}

function ApplicationErrorInspector({ node }: { node: CanvasNode }) {
  const d = node.data as ApplicationErrorNodeData;
  const update = useEditor((s) => s.updateNodeData);
  const checkpoint = useEditor((s) => s.checkpoint);
  return <>
    <div className="section"><h3>Interface</h3><h2>Error Message</h2>
      <p className="help">When the connected Condition route is active, the analytic application stops before running its workflow and displays this message. Without a condition connection, it stops every run.</p>
    </div>
    <div className="section"><label htmlFor="application-error-message">Message</label>
      <textarea id="application-error-message" aria-label="Error Message text" rows={4} value={d.message} onFocus={checkpoint}
        onChange={(e) => update(node.id, { message: e.target.value }, false)} />
    </div>
  </>;
}

function ApplicationConditionInspector({ node }: { node: CanvasNode }) {
  const d = node.data as ApplicationConditionNodeData;
  const update = useEditor((s) => s.updateNodeData);
  const checkpoint = useEditor((s) => s.checkpoint);
  const questions = useEditor((s) => s.nodes.filter((n) => n.data.kind === 'application-action')
    .map((n) => (n.data as ApplicationActionNodeData).question));
  const question = questions.find((q) => q.id === d.when.question);
  const updateWhen = (when: ApplicationConditionNodeData['when']) => update(node.id, { when }, false);
  const operators: NonNullable<ApplicationConditionNodeData['when']>['operator'][] = ['truthy', 'falsy', 'equals', 'not_equals'];
  if (question?.type === 'multi-choice') operators.push('contains', 'not_contains');
  if (question?.type === 'number' || question?.type === 'date') operators.push('greater_than', 'greater_than_or_equal', 'less_than', 'less_than_or_equal');
  const needsValue = ['equals', 'not_equals', 'contains', 'not_contains', 'greater_than', 'greater_than_or_equal', 'less_than', 'less_than_or_equal'].includes(d.when.operator);
  return <>
    <div className="section"><h3>Interface</h3><h2>Condition</h2>
      <p className="help">Routes connected Actions according to an analytic-app answer. The predicate is evaluated for each run on that run’s private workflow copy.</p>
    </div>
    <div className="section kv">
      <label>Question</label><select aria-label="Condition question" value={d.when.question} onFocus={checkpoint}
        onChange={(e) => updateWhen({ ...d.when, question: e.target.value })}>
        <option value="">Select a question</option>{questions.map((q) => <option key={q.id} value={q.id}>{q.prompt || q.id}</option>)}
      </select>
      <label>Operator</label><select aria-label="Condition operator" value={d.when.operator} onFocus={checkpoint}
        onChange={(e) => { const operator = e.target.value as ApplicationConditionNodeData['when']['operator'];
          updateWhen({ question: d.when.question, operator, ...(['equals', 'not_equals', 'contains', 'not_contains', 'greater_than', 'greater_than_or_equal', 'less_than', 'less_than_or_equal'].includes(operator)
            ? { value: question?.type === 'number' ? 0 : '' } : {}) }); }}>
        {operators.map((op) => <option key={op} value={op}>{op.replaceAll('_', ' ')}</option>)}
      </select>
      {needsValue && <><label>Compare to</label>
        {question?.type === 'boolean' ? <select aria-label="Condition value" value={String(d.when.value ?? false)} onFocus={checkpoint}
          onChange={(e) => updateWhen({ ...d.when, value: e.target.value === 'true' })}>
          <option value="true">True</option><option value="false">False</option></select>
          : <input aria-label="Condition value" type={question?.type === 'number' ? 'number' : question?.type === 'date' ? 'date' : 'text'} value={String(d.when.value ?? '')}
            onFocus={checkpoint} onChange={(e) => updateWhen({ ...d.when, value: question?.type === 'number' ? Number(e.target.value) : e.target.value })} />}
      </>}
    </div>
  </>;
}

function ApplicationActionInspector({ node }: { node: CanvasNode }) {
  const d = node.data as ApplicationActionNodeData;
  const update = useEditor((s) => s.updateNodeData);
  const checkpoint = useEditor((s) => s.checkpoint);
  const edges = useEditor((s) => s.edges);
  const nodes = useEditor((s) => s.nodes);
  const targetEdge = edges.find((e) => e.source === node.id && e.data?.kind === 'application-action');
  const target = nodes.find((n) => n.id === targetEdge?.target);
  const paths: string[][] = [];
  const walk = (value: unknown, prefix: string[] = []) => {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      for (const [key, child] of Object.entries(value)) walk(child, [...prefix, key]);
    } else if (prefix.length && (value === null || ['string', 'number', 'boolean'].includes(typeof value))) paths.push(prefix);
  };
  if (target?.data.kind === 'tool') walk(target.data.config);
  const pathKey = d.config_path.join('.');
  const knownQuestions = [...new Map(nodes.filter((n) => n.data.kind === 'application-action')
    .map((n) => { const q = (n.data as ApplicationActionNodeData).question; return [q.id, q] as const; })).values()];
  const conditionedQuestion = knownQuestions.find((q) => q.id === d.when?.question);
  const actionOperators: NonNullable<ApplicationActionNodeData['when']>['operator'][] = ['truthy', 'falsy', 'equals', 'not_equals'];
  if (conditionedQuestion?.type === 'multi-choice') actionOperators.push('contains', 'not_contains');
  if (conditionedQuestion?.type === 'number' || conditionedQuestion?.type === 'date') actionOperators.push('greater_than', 'greater_than_or_equal', 'less_than', 'less_than_or_equal');
  const changeCondition = (when: ApplicationActionNodeData['when']) => {
    update(node.id, { when }, false);
  };
  const changeQuestion = (patch: Partial<ApplicationActionNodeData['question']>) => {
    update(node.id, { question: { ...d.question, ...patch } }, false);
  };
  return <>
    <div className="section"><h3>Flowbench Action</h3><h2>Configuration action</h2>
      <p className="help">Native Flowbench analytic-app action. This editor representation is distinct from an Alteryx Action XML node; imported XML is retained separately.</p>
    </div>
    <div className="section kv">
      <label>Question ID</label><input aria-label="Action question ID" value={d.question.id} onFocus={checkpoint} onChange={(e) => changeQuestion({ id: e.target.value })} />
      <label>Prompt</label><input aria-label="Action prompt" value={d.question.prompt} onFocus={checkpoint} onChange={(e) => changeQuestion({ prompt: e.target.value })} />
      <label>Answer type</label><select aria-label="Action answer type" value={d.question.type} onFocus={checkpoint} onChange={(e) => changeQuestion({ type: e.target.value as any,
        default: e.target.value === 'text' || e.target.value === 'date' || e.target.value === 'file' || e.target.value === 'folder' ? '' : e.target.value === 'number' ? 0 : e.target.value === 'choice' ? d.question.options?.[0] ?? '' : e.target.value === 'multi-choice' ? [] : false })}>
        <option value="text">Text Box</option><option value="number">Numeric Up Down</option><option value="boolean">Check Box</option><option value="choice">Drop Down / Radio Button</option><option value="multi-choice">Multi Select</option><option value="date">Date</option><option value="file">File Browse</option><option value="folder">Folder Browse</option>
      </select>
      {(d.question.type === 'choice' || d.question.type === 'multi-choice') && <><label>Options</label><textarea aria-label="Action answer options" value={(d.question.options ?? []).map(String).join('\n')} rows={4} onFocus={checkpoint} onChange={(e) => changeQuestion({ options: e.target.value.split(/\r?\n/).filter((x) => x.length) })} /></>}
      {d.question.type === 'number' && <><label>Minimum</label><input aria-label="Action minimum" type="number" value={d.question.minimum ?? ''} onFocus={checkpoint} onChange={(e) => changeQuestion({ minimum: e.target.value === '' ? undefined : Number(e.target.value) })} /><label>Maximum</label><input aria-label="Action maximum" type="number" value={d.question.maximum ?? ''} onFocus={checkpoint} onChange={(e) => changeQuestion({ maximum: e.target.value === '' ? undefined : Number(e.target.value) })} /></>}
      <label>Required</label><input aria-label="Action answer required" type="checkbox" checked={!!d.question.required} style={{ justifySelf: 'start' }}
        onFocus={checkpoint} onChange={(e) => changeQuestion({ required: e.target.checked })} />
      <label>Default answer</label>{d.question.type === 'boolean'
        ? <input aria-label="Action default answer" type="checkbox" checked={!!d.question.default} style={{ justifySelf: 'start' }} onFocus={checkpoint} onChange={(e) => changeQuestion({ default: e.target.checked })} />
        : d.question.type === 'choice' ? <select aria-label="Action default answer" value={String(d.question.default ?? '')} onFocus={checkpoint} onChange={(e) => changeQuestion({ default: e.target.value })}><option value="">No default</option>{(d.question.options ?? []).map((o) => <option key={String(o)} value={String(o)}>{String(o)}</option>)}</select>
        : d.question.type === 'multi-choice' ? <textarea aria-label="Action default answer" rows={2} placeholder="One selected value per line" value={Array.isArray(d.question.default) ? d.question.default.join('\n') : ''} onFocus={checkpoint} onChange={(e) => changeQuestion({ default: e.target.value.split(/\r?\n/).filter(Boolean) })} />
        : <input aria-label="Action default answer" type={d.question.type === 'number' ? 'number' : 'text'} value={d.question.default == null ? '' : String(d.question.default)}
          onFocus={checkpoint} onChange={(e) => changeQuestion({ default: d.question.type === 'number' ? Number(e.target.value) : e.target.value })} />}
      <label>Condition question</label>
      <select aria-label="Action condition question" value={d.when?.question ?? ''} onFocus={checkpoint}
        onChange={(e) => changeCondition(e.target.value ? { question: e.target.value, operator: 'truthy' } : undefined)}>
        <option value="">Always apply</option>
        {knownQuestions.map((q) => <option key={q.id} value={q.id}>{q.prompt || q.id}</option>)}
      </select>
      {d.when && <><label>Condition</label>
        <select aria-label="Action condition operator" value={d.when.operator} onFocus={checkpoint}
          onChange={(e) => {
            const operator = e.target.value as NonNullable<ApplicationActionNodeData['when']>['operator'];
            changeCondition({ question: d.when!.question, operator,
              ...(['equals', 'not_equals', 'contains', 'not_contains', 'greater_than', 'greater_than_or_equal', 'less_than', 'less_than_or_equal'].includes(operator)
                ? { value: conditionedQuestion?.type === 'number' ? 0 : '' } : {}) });
          }}>
          {actionOperators.map((operator) => <option key={operator} value={operator}>{operator.replaceAll('_', ' ')}</option>)}
        </select>
        {['equals', 'not_equals', 'contains', 'not_contains', 'greater_than', 'greater_than_or_equal', 'less_than', 'less_than_or_equal'].includes(d.when.operator) &&
          <><label>Compare to</label>
            {conditionedQuestion?.type === 'boolean'
              ? <select aria-label="Action condition value" value={String(d.when.value ?? false)} onFocus={checkpoint}
                  onChange={(e) => changeCondition({ ...d.when!, value: e.target.value === 'true' })}>
                  <option value="true">True</option><option value="false">False</option></select>
              : <input aria-label="Action condition value" type={conditionedQuestion?.type === 'number' ? 'number' : conditionedQuestion?.type === 'date' ? 'date' : 'text'}
                  value={String(d.when.value ?? '')} onFocus={checkpoint}
                  onChange={(e) => changeCondition({ ...d.when!, value: conditionedQuestion?.type === 'number' ? Number(e.target.value) : e.target.value })} />}
          </>}
      </>}
      <label>Target</label><span>{target?.data.kind === 'tool' ? target.id : 'Connect Action to a tool’s Action anchor'}</span>
      <label>Configuration field</label><select aria-label="Action configuration field" value={pathKey} disabled={!target} onFocus={checkpoint}
        onChange={(e) => update(node.id, { config_path: e.target.value.split('.') })}>
        {!paths.some((p) => p.join('.') === pathKey) && <option value={pathKey}>{pathKey || 'Choose a field'}</option>}
        {paths.map((p) => <option key={p.join('.')} value={p.join('.')}>{p.join('.')}</option>)}
      </select>
    </div>
    <div className="section"><p className="help">Run prompts for the question, applies the answer to the selected target field on a private workflow copy, validates, then executes. Action links are configuration dependencies and are not row-data connections.</p></div>
  </>;
}

function MultiSelection({ count, embedded = false }: { count: number; embedded?: boolean }) {
  const group = useEditor((s) => s.groupSelected);
  const del = useEditor((s) => s.deleteSelected);
  const dup = useEditor((s) => s.duplicateSelected);
  return (
    <aside className={embedded ? 'inspector-content' : 'app-right'} aria-label="Configuration" data-testid="inspector">
      <div className="section"><h2>{count} items selected</h2></div>
      <div className="section" style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        <button onClick={() => group()}>Group in container (Ctrl+G)</button>
        <button onClick={() => dup()}>Duplicate (Ctrl+D)</button>
        <button className="danger" onClick={() => del()}>Delete</button>
      </div>
    </aside>
  );
}

function WorkflowProperties() {
  const name = useEditor((s) => s.name);
  const profile = useEditor((s) => s.profile);
  const profiles = useEditor((s) => s.profiles);
  const baseDir = useEditor((s) => s.baseDir);
  const filePath = useEditor((s) => s.filePath);
  const nodes = useEditor((s) => s.nodes);
  const problems = useEditor((s) => s.problems);
  const p = profiles.find((x) => x.name === profile);
  return (
    <>
      <div className="section"><h3>Workflow</h3><h2>{name}</h2></div>
      <div className="section kv">
        <label>File</label><span style={{ wordBreak: 'break-all' }}>{filePath ?? 'not saved'}</span>
        <label htmlFor="basedir">Base folder</label>
        <input id="basedir" type="text" value={baseDir} onChange={(e) => useEditor.getState().setFilePath(filePath, e.target.value)}
          title="Relative input/output paths resolve against this folder" />
        <label>Tools</label><span>{nodes.filter((n) => n.data.kind === 'tool').length}</span>
      </div>
      <div className="section">
        <h3>Compatibility profile</h3>
        <select value={profile} onChange={(e) => useEditor.getState().setProfile(e.target.value)} aria-label="Compatibility profile">
          {profiles.map((x) => <option key={x.name} value={x.name}>{x.name} ({x.status})</option>)}
        </select>
        {p && <p style={{ color: 'var(--text-2)', fontSize: 12 }}>{p.notes.join(' ')}</p>}
      </div>
      <div className="section">
        <h3>Validation</h3>
        {problems.length ? problems.map((pr, i) => <ProblemRow key={i} p={pr} />) : <div style={{ color: 'var(--ok)' }}>No problems found.</div>}
      </div>
    </>
  );
}

export function ProblemRow({ p }: { p: { code: string; message: string; node_id: string | null } }) {
  return (
    <div className="problem" role="alert">
      {p.node_id && <strong>{p.node_id}: </strong>}{p.message} <code>({p.code})</code>
    </div>
  );
}

function upstreamFields(node: CanvasNode): FieldContext {
  const s = useEditor.getState();
  const byAnchor: Record<string, FieldDef[]> = {};
  for (const e of s.edges.filter((x) => x.target === node.id)) {
    let fs = s.schemas[e.source]?.[e.sourceHandle ?? 'output'] ?? [];
    // Source tools with explicit field declarations know their schema before a
    // run. Keep field selectors useful while a workflow is being configured.
    if (!fs.length) {
      const source = s.nodes.find((n) => n.id === e.source);
      if (source?.data.kind === 'tool' && ['core.text_input', 'core.input_data'].includes(source.data.tool)) {
        const configured = source.data.config.fields;
        if (Array.isArray(configured)) fs = configured.filter((f: any) => f && typeof f.name === 'string')
          .map((f: any) => ({ name: f.name, type: String(f.type ?? 'v_wstring'),
            ...(Number.isFinite(f.size) ? { size: f.size } : {}),
            ...(Number.isFinite(f.scale) ? { scale: f.scale } : {}) }));
      }
    }
    byAnchor[e.targetHandle ?? 'input'] = [...(byAnchor[e.targetHandle ?? 'input'] ?? []), ...fs];
  }
  const seen = new Set<string>();
  const all: FieldDef[] = [];
  for (const fs of Object.values(byAnchor)) for (const f of fs) if (!seen.has(f.name)) { seen.add(f.name); all.push(f); }
  return { all, byAnchor };
}

function ToolInspector({ node }: { node: CanvasNode }) {
  const d = node.data as ToolNodeData;
  const manifest = useEditor((s) => s.tools[d.tool]);
  const entry = useEditor((s) => s.catalog.find((c) => manifest?.catalog_ids.includes(c.id)));
  const problems = useEditor((s) => s.problems.filter((p) => p.node_id === node.id));
  const outSchemas = useEditor((s) => s.schemas[node.id]);
  const edges = useEditor((s) => s.edges);
  const schemasVersion = useEditor((s) => s.schemas);
  const update = useEditor((s) => s.updateNodeData);
  const checkpoint = useEditor((s) => s.checkpoint);
  const editing = useRef<string | null>(null);
  const fields = useMemo(() => upstreamFields(node), [node, edges, schemasVersion]);
  const beginEdit = () => {
    if (editing.current !== node.id) {
      checkpoint();
      editing.current = node.id;
    }
  };
  if (!manifest) {
    return <div className="section"><h2>{d.tool}</h2><div className="problem">No implementation is registered for this tool.</div></div>;
  }
  const color = categoryColor(entry?.category);
  return (
    <div onBlur={() => { editing.current = null; }}>
      <div className="section" style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
        <div className="glyph" style={{ background: color, color: '#fff', width: 30, height: 30, borderRadius: 7, display: 'grid', placeItems: 'center', fontWeight: 700 }}>{glyph(manifest.display_name)}</div>
        <div><h2>{manifest.display_name}</h2><div style={{ color: 'var(--text-3)', fontSize: 11 }}>{entry?.category} · {manifest.tool_id} v{manifest.version}</div></div>
      </div>
      <div className="section kv">
        <label>Node id</label><code>{node.id}</code>
        <label htmlFor="node-label">Label</label>
        <input id="node-label" type="text" value={d.label ?? ''} placeholder={manifest.display_name} onFocus={beginEdit}
          onChange={(e) => update(node.id, { label: e.target.value || undefined }, false)} />
        <label htmlFor="node-annot">Annotation</label>
        <input id="node-annot" type="text" value={d.annotation ?? ''} onFocus={beginEdit}
          onChange={(e) => update(node.id, { annotation: e.target.value || undefined }, false)} />
        <label htmlFor="node-disabled">Disabled</label>
        <input id="node-disabled" type="checkbox" checked={!!d.disabled} style={{ justifySelf: 'start' }}
          onChange={(e) => update(node.id, { disabled: e.target.checked || undefined })} />
      </div>
      {problems.length > 0 && <div className="section">{problems.map((p, i) => <ProblemRow key={i} p={p} />)}</div>}
      <div className="section" data-testid="config-form">
        <h3>Configuration</h3>
        {manifest.tool_id === 'core.text_input'
          ? <TextInputEditor value={d.config} onBeginEdit={beginEdit} onChange={(config) => update(node.id, { config }, false)} />
          : manifest.tool_id === 'core.db_input'
            ? <DatabaseInputForm value={d.config} onBeginEdit={beginEdit} onChange={(config) => update(node.id, { config }, false)} />
          : manifest.tool_id === 'core.db_output'
            ? <DatabaseOutputForm value={d.config} onBeginEdit={beginEdit} onChange={(config) => update(node.id, { config }, false)} />
          : <SchemaForm schema={manifest.config_schema} value={d.config} fields={fields} idPrefix={`cfg-${node.id}`}
            onBeginEdit={beginEdit} onChange={(config) => update(node.id, { config }, false)} />}
        {manifest.tool_id === 'core.input_data' && ['csv', 'tsv', 'delimited', 'auto'].includes(String(d.config.format ?? 'auto')) &&
          <p className="help" role="note">Flowbench mode infers types across the complete file and keeps identifier fields (such as IDs and postcodes) as text. Use Fields to override a type, or choose Alteryx compatibility to read delimited values as text.</p>}
        {manifest.tool_id === 'core.filter' && (() => {
          const fieldName = typeof d.config.field === 'string' ? d.config.field : '';
          const selectedField = fields.all.find((f) => f.name === fieldName);
          return selectedField && ['string', 'wstring', 'v_string', 'v_wstring'].includes(selectedField.type)
            && ['<', '<=', '>', '>='].includes(String(d.config.operator))
            ? <p className="help" role="note">{fieldName} is {selectedField.type}; this comparison is lexicographic text ordering (so "2" is greater than "10"). Preserve identifiers as text, or convert this field with Select before numeric comparison.</p>
            : null;
        })()}
        <p className="help" style={{ color: 'var(--text-3)', fontSize: 11 }}>{manifest.description}</p>
      </div>
      <div className="section">
        <h3>Anchors</h3>
        <table className="schema-table">
          <thead><tr><th>Anchor</th><th>Kind</th><th>Connections</th></tr></thead>
          <tbody>
            {manifest.inputs.map((a) => (
              <tr key={'i' + a.name}><td>{a.label} <code>{a.name}</code></td><td>input{a.multiple ? ' (many)' : ''}{a.required ? '' : ' (optional)'}</td>
                <td>{edges.filter((e) => e.target === node.id && e.targetHandle === a.name).length}</td></tr>
            ))}
            {manifest.outputs.map((a) => (
              <tr key={'o' + a.name}><td>{a.label} <code>{a.name}</code></td><td>output</td>
                <td>{edges.filter((e) => e.source === node.id && e.sourceHandle === a.name).length}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <SchemaPreview title="Incoming fields" groups={fields.byAnchor} />
      <SchemaPreview title="Outgoing fields" groups={outSchemas ?? {}} emptyText="Not known until upstream inputs are valid (or data-dependent)." />
    </div>
  );
}

type DbConnection = { id: string; name: string; dialect: string; allow_writes?: boolean };
type DbTable = { name: string; kind: string };
function DatabaseInputForm({ value, onBeginEdit, onChange }: { value: Record<string, unknown>; onBeginEdit: () => void;
  onChange: (v: Record<string, unknown>) => void }) {
  const [connections, setConnections] = useState<DbConnection[]>([]);
  const [schemas, setSchemas] = useState<string[]>([]);
  const [tables, setTables] = useState<DbTable[]>([]);
  const [preview, setPreview] = useState<any>(null);
  const [error, setError] = useState('');
  const connection = String(value.connection_id ?? '');
  const schema = String(value.schema ?? '');
  const mode = String(value.mode ?? 'table');
  useEffect(() => { void engine().request<DbConnection[]>('database.connections.list').then(setConnections).catch((e) => setError(describe(e))); }, []);
  useEffect(() => {
    if (!connection) { setSchemas([]); setTables([]); return; }
    void engine().request<string[]>('database.schemas', { connection_id: connection }).then((x) => {
      setSchemas(x); if (!x.includes(schema)) setTables([]);
    }).catch((e) => setError(describe(e)));
  }, [connection]);
  useEffect(() => {
    if (!connection || !schema) { setTables([]); return; }
    void engine().request<DbTable[]>('database.tables', { connection_id: connection, schema }).then(setTables).catch((e) => setError(describe(e)));
  }, [connection, schema]);
  const change = (patch: Record<string, unknown>) => {
    onBeginEdit(); onChange({ ...value, mode: patch.mode ?? value.mode ?? 'table', ...patch }); setPreview(null); setError('');
  };
  const loadPreview = async () => {
    setError('');
    try { setPreview(await engine().request('database.preview', { connection_id: connection, mode,
      schema: value.schema, table: value.table, query: value.query, params: value.params ?? [], limit: 100 })); }
    catch (e) { setError(describe(e)); }
  };
  return <div className="section db-input-form">
    <label>Connection<select value={connection} onFocus={onBeginEdit} onChange={(e) => change({ connection_id: e.target.value, schema: null, table: undefined })}>
      <option value="">Choose a saved connection</option>{connections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.dialect})</option>)}
    </select></label>
    <label>Source<select value={mode} onFocus={onBeginEdit} onChange={(e) => change({ mode: e.target.value, ...(e.target.value === 'query' ? {} : { query: undefined }) })}>
      <option value="table">Table or view</option><option value="query">Read-only SQL query</option></select></label>
    {mode === 'table' ? <>
      <label>Schema<select value={schema} onFocus={onBeginEdit} onChange={(e) => change({ schema: e.target.value || null, table: undefined })}>
        <option value="">Default schema</option>{schemas.map((s) => <option key={s}>{s}</option>)}</select></label>
      <label>Table or view<select value={String(value.table ?? '')} onFocus={onBeginEdit} onChange={(e) => change({ table: e.target.value })}>
        <option value="">Choose a table or view</option>{tables.map((t) => <option key={t.name} value={t.name}>{t.name} ({t.kind})</option>)}</select></label>
    </> : <label>Read-only SQL<textarea rows={7} value={String(value.query ?? '')} onFocus={onBeginEdit}
      onChange={(e) => change({ query: e.target.value })} placeholder="SELECT …" /></label>}
    <button onClick={() => void loadPreview()} disabled={!connection || (mode === 'table' ? !value.table : !value.query)}>Preview live data</button>
    {error && <div className="problem" role="alert">{error}</div>}
    {preview && <div className="db-preview"><strong>{preview.total_rows} row(s) · {preview.fields.map((f: FieldDef) => f.name).join(', ')}</strong>
      <div className="db-preview-table">{preview.rows.map((r: unknown[], i: number) => <div key={i}>{r.map((x, j) => <span key={j}>{x == null ? '∅' : String(x)}</span>)}</div>)}</div></div>}
    <p className="help">Rows are fetched from the database each time the workflow runs. Read-only SQL accepts one SELECT statement with parameter placeholders.</p>
  </div>;
}

function DatabaseOutputForm({ value, onBeginEdit, onChange }: { value: Record<string, unknown>; onBeginEdit: () => void;
  onChange: (v: Record<string, unknown>) => void }) {
  const [connections, setConnections] = useState<DbConnection[]>([]);
  const [error, setError] = useState('');
  useEffect(() => { void engine().request<DbConnection[]>('database.connections.list').then(setConnections)
    .catch((e) => setError(describe(e))); }, []);
  const change = (patch: Record<string, unknown>) => { onBeginEdit(); onChange({ ...value, ...patch }); };
  const connection = connections.find((c) => c.id === value.connection_id);
  return <div className="section db-output-form">
    <label>Write-enabled connection<select value={String(value.connection_id ?? '')} onFocus={onBeginEdit}
      onChange={(e) => change({ connection_id: e.target.value })}>
      <option value="">Choose a connection</option>{connections.map((c) => <option key={c.id} value={c.id}
        disabled={!c.allow_writes}>{c.name} ({c.dialect}){c.allow_writes ? '' : ' — read only'}</option>)}
    </select></label>
    {connection && !connection.allow_writes && <div className="problem" role="alert">Enable write capability for this connection in Database connections.</div>}
    <label>Schema<input value={String(value.schema ?? '')} onFocus={onBeginEdit}
      onChange={(e) => change({ schema: e.target.value || null })} placeholder="Default schema" /></label>
    <label>Table<input value={String(value.table ?? '')} onFocus={onBeginEdit}
      onChange={(e) => change({ table: e.target.value })} /></label>
    <label>Write mode<select value={String(value.mode ?? 'append')} onFocus={onBeginEdit}
      onChange={(e) => change({ mode: e.target.value })}>
      <option value="append">Append to existing table</option><option value="create">Create new table</option></select></label>
    <label>Batch size<input type="number" min={1} value={Number(value.batch_rows ?? 1000)} onFocus={onBeginEdit}
      onChange={(e) => change({ batch_rows: Number(e.target.value) })} /></label>
    {error && <div className="problem" role="alert">{error}</div>}
    <p className="help">Writes run in one transaction and are rolled back on error. The connection must have the database permissions required for this action, and the desktop asks for confirmation on every workflow run.</p>
  </div>;
}

function SchemaPreview({ title, groups, emptyText }: { title: string; groups: Record<string, FieldDef[]>; emptyText?: string }) {
  const entries = Object.entries(groups);
  return (
    <div className="section">
      <h3>{title}</h3>
      {!entries.length && <div style={{ color: 'var(--text-3)', fontSize: 12 }}>{emptyText ?? 'None connected.'}</div>}
      {entries.map(([anchor, fs]) => (
        <div key={anchor} style={{ marginBottom: 8 }}>
          {entries.length > 1 && <div style={{ fontWeight: 600, fontSize: 12 }}>{anchor}</div>}
          <table className="schema-table">
            <thead><tr><th>Field</th><th>Type</th><th>Size</th></tr></thead>
            <tbody>{fs.map((f) => <tr key={f.name}><td>{f.name}</td><td>{f.type}</td><td>{f.size == null ? '' : f.size >= 1073741823 ? 'Unbounded' : f.size}{f.scale !== undefined ? `.${f.scale}` : ''}</td></tr>)}</tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

const TYPES = ['bool', 'byte', 'int16', 'int32', 'int64', 'fixed_decimal', 'float', 'double', 'string', 'wstring', 'v_string', 'v_wstring', 'date', 'time', 'datetime'];

function TextInputEditor({ value, onChange, onBeginEdit }: { value: Record<string, unknown>; onChange: (v: Record<string, unknown>) => void; onBeginEdit: () => void }) {
  const fields = (value.fields as { name: string; type: string }[]) ?? [];
  const rows = (value.rows as (string | number | boolean | null)[][]) ?? [];
  const set = (f: typeof fields, r: typeof rows) => onChange({ ...value, fields: f, rows: r });
  return (
    <div data-testid="text-input-editor" onFocus={onBeginEdit}>
      <div style={{ overflowX: 'auto' }}>
        <table className="schema-table">
          <thead>
            <tr>
              {fields.map((f, i) => (
                <th key={i} style={{ minWidth: 110 }}>
                  <input type="text" aria-label={`Field ${i + 1} name`} value={f.name} onChange={(e) => set(fields.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)), rows)} />
                  <select aria-label={`Field ${i + 1} type`} value={f.type} onChange={(e) => set(fields.map((x, j) => (j === i ? { ...x, type: e.target.value } : x)), rows)}>
                    {TYPES.map((t) => <option key={t}>{t}</option>)}
                  </select>
                  <button type="button" className="icon" aria-label={`Remove field ${i + 1}`} onClick={() => set(fields.filter((_, j) => j !== i), rows.map((r) => r.filter((_, j) => j !== i)))}>×</button>
                </th>
              ))}
              <th><button type="button" onClick={() => set([...fields, { name: `Field${fields.length + 1}`, type: 'v_wstring' }], rows.map((r) => [...r, null]))}>+ field</button></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                {fields.map((_, j) => (
                  <td key={j}>
                    <input type="text" aria-label={`Row ${i + 1} field ${j + 1}`} value={r[j] === null || r[j] === undefined ? '' : String(r[j])}
                      placeholder="null" onChange={(e) => set(fields, rows.map((rr, ii) => (ii === i ? rr.map((c, jj) => (jj === j ? (e.target.value === '' ? null : e.target.value) : c)) : rr)))} />
                  </td>
                ))}
                <td><button type="button" className="icon" aria-label={`Remove row ${i + 1}`} onClick={() => set(fields, rows.filter((_, ii) => ii !== i))}>×</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button type="button" style={{ marginTop: 6 }} onClick={() => set(fields, [...rows, fields.map(() => null)])} disabled={!fields.length}>+ row</button>
      <div className="help" style={{ color: 'var(--text-3)', fontSize: 11 }}>Empty cells are null. Values are converted to the field type when the workflow runs.</div>
    </div>
  );
}

function ContainerInspector({ node }: { node: CanvasNode }) {
  const d = node.data as { caption: string; disabled?: boolean };
  const update = useEditor((s) => s.updateNodeData);
  const ungroup = useEditor((s) => s.ungroup);
  const count = useEditor((s) => s.nodes.filter((n) => n.parentId === node.id).length);
  return (
    <>
      <div className="section"><h3>Container</h3><h2>{d.caption}</h2></div>
      <div className="section kv">
        <label htmlFor="cap">Caption</label>
        <input id="cap" type="text" value={d.caption} onChange={(e) => update(node.id, { caption: e.target.value }, false)} />
        <label htmlFor="cdis">Disabled</label>
        <input id="cdis" type="checkbox" style={{ justifySelf: 'start' }} checked={!!d.disabled} onChange={(e) => update(node.id, { disabled: e.target.checked })} />
        <label>Contents</label><span>{count} item(s)</span>
      </div>
      <div className="section"><p style={{ color: 'var(--text-2)', fontSize: 12 }}>Disabling a container disables every tool inside it and everything downstream of them.</p>
        <button onClick={() => ungroup(node.id)}>Ungroup</button></div>
    </>
  );
}

function CommentInspector({ node }: { node: CanvasNode }) {
  const d = node.data as { text: string };
  const update = useEditor((s) => s.updateNodeData);
  return (
    <>
      <div className="section"><h3>Comment</h3></div>
      <div className="section"><textarea rows={6} value={d.text} aria-label="Comment text" onChange={(e) => update(node.id, { text: e.target.value }, false)} /></div>
    </>
  );
}
