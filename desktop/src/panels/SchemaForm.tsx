import { useRef } from 'react';
import type { FieldDef, JsonSchema } from '../engine/types';

// Property names that hold a field name / list of field names / an expression (engine conventions).
const FIELD_KEYS = /^(field|header_field|value_field|left|right|rename_from|record_id_field|source_id_field|blob_field|filename_field|path_field)$/;
const FIELD_LIST_KEYS = /^(fields|group_by|key_fields|data_fields|right_fields|left_fields)$/;
const EXPR_KEYS = /^(expression|init|condition|loop)$/;
const LABELS: Record<string, string> = { group_by: 'Group by', on_conversion_error: 'On conversion error' };

export type FieldContext = { all: FieldDef[]; byAnchor: Record<string, FieldDef[]> };

type Props = {
  schema: JsonSchema;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
  onBeginEdit: () => void;
  fields: FieldContext;
  idPrefix: string;
};

function humanize(k: string) {
  return LABELS[k] ?? k.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

function types(s: JsonSchema): string[] {
  return Array.isArray(s.type) ? s.type : s.type ? [s.type] : [];
}

export function SchemaForm({ schema, value, onChange, onBeginEdit, fields, idPrefix }: Props) {
  const props = schema.properties ?? {};
  const variants = schema.oneOf?.filter((v) => v.required?.length) ?? [];
  let hidden = new Set<string>();
  let mode = -1;
  if (variants.length > 1) {
    mode = variants.findIndex((v) => v.required!.every((k) => value[k] !== undefined));
    if (mode < 0) mode = 0;
    for (const [i, v] of variants.entries()) if (i !== mode) v.required!.forEach((k) => hidden.add(k));
    variants[mode].required!.forEach((k) => hidden.delete(k));
    if (mode === 0 && variants[0].required!.includes('expression')) hidden.add('value');
  }
  const setMode = (i: number) => {
    onBeginEdit();
    const next: Record<string, unknown> = { ...value };
    for (const [j, v] of variants.entries()) if (j !== i) v.required!.forEach((k) => delete next[k]);
    if (variants[i].required!.includes('expression')) delete next.value;
    for (const k of variants[i].required!) if (next[k] === undefined) next[k] = props[k]?.enum ? props[k].enum![0] : '';
    onChange(next);
  };
  return (
    <div>
      {variants.length > 1 && (
        <div className="form-field">
          <label>Mode</label>
          <div className="chips" role="radiogroup">
            {variants.map((v, i) => (
              <button key={i} type="button" role="radio" aria-checked={i === mode} className={`chip ${i === mode ? 'on' : ''}`}
                onClick={() => setMode(i)}>{v.required!.includes('expression') ? 'Custom expression' : 'Basic: ' + v.required!.join(' + ')}</button>
            ))}
          </div>
        </div>
      )}
      {Object.entries(props).filter(([k]) => !hidden.has(k)).map(([k, ps]) => (
        <Field key={k} name={k} schema={ps} required={!!schema.required?.includes(k)} value={value[k]} fields={fields}
          id={`${idPrefix}-${k}`} onBeginEdit={onBeginEdit}
          onChange={(v) => {
            const next = { ...value };
            if (v === undefined) delete next[k];
            else next[k] = v;
            onChange(next);
          }} />
      ))}
    </div>
  );
}

type FieldProps = {
  name: string; schema: JsonSchema; required: boolean; value: unknown; fields: FieldContext; id: string;
  onChange: (v: unknown) => void; onBeginEdit: () => void;
};

function Field({ name, schema, required, value, fields, id, onChange, onBeginEdit }: FieldProps) {
  const t = types(schema);
  const nullable = t.includes('null');
  const main = t.find((x) => x !== 'null') ?? (schema.enum ? 'enum' : 'string');
  const label = <label htmlFor={id}>{humanize(name)}{required ? ' *' : ''}</label>;
  const help = schema.description ? <div className="help">{schema.description}</div> : null;
  const anchorFields = fields.byAnchor[name] ?? fields.byAnchor[name.replace(/_fields$/, '')] ?? fields.all;

  if (nullable && value === null) {
    return (
      <div className="form-field">{label}
        <div><button type="button" onClick={() => { onBeginEdit(); onChange(main === 'array' ? [] : main === 'integer' ? 0 : ''); }}>Set value</button>
          <span style={{ color: 'var(--text-3)', marginLeft: 8 }}>not set{schema.default === null ? ' (default)' : ''}</span></div>{help}
      </div>
    );
  }
  const clear = nullable ? <button type="button" className="icon" title="Unset" onClick={() => { onBeginEdit(); onChange(null); }}>×</button> : null;

  if (schema.enum) {
    return (
      <div className="form-field">{label}
        <select id={id} value={String(value ?? schema.default ?? '')} onFocus={onBeginEdit}
          onChange={(e) => onChange(schema.enum!.find((x) => String(x) === e.target.value))}>
          {!required && value === undefined && schema.default === undefined && <option value="">—</option>}
          {schema.enum.map((x) => <option key={String(x)} value={String(x)}>{x === '\n' ? 'LF' : x === '\r\n' ? 'CRLF' : String(x)}</option>)}
        </select>{help}
      </div>
    );
  }
  if (main === 'boolean') {
    return (
      <div className="form-field">
        <label style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <input id={id} type="checkbox" checked={Boolean(value ?? schema.default)} onChange={(e) => { onBeginEdit(); onChange(e.target.checked); }} />
          {humanize(name)}
        </label>{help}
      </div>
    );
  }
  if (main === 'integer' || main === 'number') {
    return (
      <div className="form-field">{label}
        <div style={{ display: 'flex', gap: 4 }}>
          <input id={id} type="number" value={value === undefined ? '' : String(value)} placeholder={schema.default !== undefined ? String(schema.default) : ''}
            step={main === 'integer' ? 1 : 'any'} onFocus={onBeginEdit}
            onChange={(e) => onChange(e.target.value === '' ? undefined : main === 'integer' ? parseInt(e.target.value, 10) : parseFloat(e.target.value))} />
          {clear}
        </div>{help}
      </div>
    );
  }
  if (main === 'array') {
    const items = (value as unknown[] | undefined) ?? [];
    const itemSchema = schema.items ?? {};
    if (types(itemSchema).includes('string') && !itemSchema.enum && FIELD_LIST_KEYS.test(name)) {
      const picked = new Set(items as string[]);
      const known = anchorFields.map((f) => f.name);
      const extra = (items as string[]).filter((x) => !known.includes(x));
      return (
        <div className="form-field">{label}
          <div className="chips" data-testid={`${id}-chips`}>
            {[...known, ...extra].map((f) => (
              <button type="button" key={f} className={`chip ${picked.has(f) ? 'on' : ''}`} aria-pressed={picked.has(f)}
                title={extra.includes(f) ? 'Not present in the incoming data' : undefined}
                style={extra.includes(f) ? { borderColor: 'var(--err)' } : undefined}
                onClick={() => { onBeginEdit(); onChange(picked.has(f) ? (items as string[]).filter((x) => x !== f) : [...(items as string[]), f]); }}>{f}</button>
            ))}
            {!known.length && !extra.length && <span style={{ color: 'var(--text-3)' }}>Connect an input to choose fields</span>}
            {clear}
          </div>{help}
        </div>
      );
    }
    if (itemSchema.type === 'object' || itemSchema.properties) {
      return (
        <div className="form-field">{label}
          {items.map((it, i) => (
            <div className="array-item" key={i}>
              <button type="button" className="remove icon" aria-label={`Remove ${humanize(name)} ${i + 1}`}
                onClick={() => { onBeginEdit(); onChange(items.filter((_, j) => j !== i)); }}>×</button>
              <SchemaForm schema={itemSchema} value={(it as Record<string, unknown>) ?? {}} fields={fields} idPrefix={`${id}-${i}`}
                onBeginEdit={onBeginEdit} onChange={(v) => onChange(items.map((x, j) => (j === i ? v : x)))} />
            </div>
          ))}
          <button type="button" onClick={() => { onBeginEdit(); onChange([...items, defaultsFor(itemSchema)]); }}>+ Add {humanize(name).replace(/s$/, '').toLowerCase()}</button>
          {help}
        </div>
      );
    }
    // array of scalars (e.g. Text Input rows) -> JSON editor
    return <JsonEditor id={id} label={label} help={help} value={items} onBeginEdit={onBeginEdit} onChange={onChange} />;
  }
  if (main === 'string' && EXPR_KEYS.test(name)) {
    return <ExpressionField id={id} label={label} help={help} value={String(value ?? '')} fields={anchorFields}
      onBeginEdit={onBeginEdit} onChange={onChange} />;
  }
  const declaresOutputField = name === 'field' &&
    (id.includes('-formulas-') || id.includes('-generate_rows-field') || id.includes('-multi_row_formula-field'));
  if (main === 'string' && FIELD_KEYS.test(name) && !declaresOutputField && anchorFields.length) {
    const known = anchorFields.map((f) => f.name);
    const v = String(value ?? '');
    return (
      <div className="form-field">{label}
        <select id={id} value={v} onFocus={onBeginEdit} onChange={(e) => onChange(e.target.value)}
          style={v && !known.includes(v) ? { borderColor: 'var(--err)' } : undefined}>
          {!v && <option value="">Choose a field…</option>}
          {v && !known.includes(v) && <option value={v}>{v} (missing)</option>}
          {anchorFields.map((f) => <option key={f.name} value={f.name}>{f.name} — {f.type}</option>)}
        </select>{help}
      </div>
    );
  }
  if (main === 'object' && schema.properties) {
    return (
      <div className="form-field">{label}
        <div className="array-item">
          <SchemaForm schema={schema} value={(value as Record<string, unknown>) ?? {}} fields={fields} idPrefix={id}
            onBeginEdit={onBeginEdit} onChange={onChange} />
        </div>
      </div>
    );
  }
  if (main === 'string' || t.length > 1) {
    return (
      <div className="form-field">{label}
        <div style={{ display: 'flex', gap: 4 }}>
          <input id={id} type="text" value={value === undefined || value === null ? '' : String(value)}
            placeholder={schema.default !== undefined && schema.default !== null ? String(schema.default) : ''}
            onFocus={onBeginEdit} onChange={(e) => onChange(coerce(e.target.value, t))} />
          {clear}
        </div>{help}
      </div>
    );
  }
  return <JsonEditor id={id} label={label} help={help} value={value} onBeginEdit={onBeginEdit} onChange={onChange} />;
}

function coerce(text: string, t: string[]): unknown {
  if (t.includes('string')) return text;
  if (text === '') return undefined;
  if ((t.includes('number') || t.includes('integer')) && !isNaN(Number(text))) return Number(text);
  if (t.includes('boolean') && (text === 'true' || text === 'false')) return text === 'true';
  return text;
}

function defaultsFor(s: JsonSchema): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of s.required ?? []) {
    const p = s.properties?.[k] ?? {};
    out[k] = p.default !== undefined ? p.default : p.enum ? p.enum[0] : p.type === 'array' ? [] : '';
  }
  return out;
}

function ExpressionField({ id, label, help, value, fields, onChange, onBeginEdit }: {
  id: string; label: JSX.Element; help: JSX.Element | null; value: string; fields: FieldDef[];
  onChange: (v: unknown) => void; onBeginEdit: () => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const insert = (text: string) => {
    onBeginEdit();
    const el = ref.current;
    if (!el) return onChange(value + text);
    const s = el.selectionStart ?? value.length;
    const e = el.selectionEnd ?? value.length;
    onChange(value.slice(0, s) + text + value.slice(e));
    requestAnimationFrame(() => { el.focus(); el.setSelectionRange(s + text.length, s + text.length); });
  };
  return (
    <div className="form-field expr">{label}
      <textarea id={id} ref={ref} rows={3} value={value} spellCheck={false} onFocus={onBeginEdit}
        onChange={(e) => onChange(e.target.value)} data-testid={`${id}-expr`} />
      {fields.length > 0 && (
        <div className="fields" aria-label="Insert field">
          {fields.map((f) => <button type="button" key={f.name} title={f.type} onClick={() => insert(`[${f.name}]`)}>{f.name}</button>)}
        </div>
      )}
      {help}
    </div>
  );
}

function JsonEditor({ id, label, help, value, onChange, onBeginEdit }: {
  id: string; label: JSX.Element; help: JSX.Element | null; value: unknown; onChange: (v: unknown) => void; onBeginEdit: () => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const text = JSON.stringify(value ?? null, null, 1);
  return (
    <div className="form-field">{label}
      <textarea id={id} ref={ref} rows={5} defaultValue={text} key={text} spellCheck={false} onFocus={onBeginEdit}
        onBlur={(e) => {
          try {
            const v = JSON.parse(e.target.value);
            onChange(v);
            e.target.style.borderColor = '';
          } catch {
            e.target.style.borderColor = 'var(--err)';
          }
        }} />
      <div className="help">JSON value; applied when the editor loses focus.</div>{help}
    </div>
  );
}
