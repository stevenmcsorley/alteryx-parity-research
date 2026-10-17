import { tokens, USAGE_PARTS, type Usage } from './panelText';

/** A stacked bar of where a turn's (or conversation's) tokens went, coloured by kind. */
export function UsageBar({ usage, compact = false }: { usage: Usage; compact?: boolean }) {
  const total = usage.input + usage.output;
  return <span className={`ai-usage-bar ${compact ? 'compact' : ''}`} role="img"
    aria-label={USAGE_PARTS.map((p) => `${p.label} ${tokens(usage[p.key])}`).join(', ')}>
    {USAGE_PARTS.map((p) => usage[p.key] > 0 && <span key={p.key} className={`ai-tok-${p.key}`}
      style={{ width: `${(100 * usage[p.key]) / Math.max(1, total)}%` }} />)}
  </span>;
}

/** Bar, totals and a coloured legend for one scope (this turn, this conversation). */
export function UsageBreakdown({ title, usage }: { title: string; usage: Usage }) {
  const total = usage.input + usage.output;
  return <div className="ai-usage-breakdown" aria-label={`${title} token usage`}>
    <div className="ai-usage-head"><span>{title}</span><b>{tokens(total)}</b>
      <span className="ai-usage-steps">{usage.steps} step{usage.steps === 1 ? '' : 's'}</span></div>
    <UsageBar usage={usage} />
    <ul className="ai-usage-legend">
      {USAGE_PARTS.map((p) => <li key={p.key} title={p.note}>
        <i className={`ai-tok-${p.key}`} aria-hidden="true" />
        <span>{p.label}</span>
        <b>{tokens(usage[p.key])}</b>
        <span className="ai-usage-share">{total ? `${Math.round((100 * usage[p.key]) / total)}%` : ''}</span>
      </li>)}
      {usage.reasoning > 0 && <li className="ai-usage-sub" title="Part of Generated: tokens the model spent reasoning">
        <i aria-hidden="true" /><span>of which reasoning</span><b>{tokens(usage.reasoning)}</b><span /></li>}
    </ul>
  </div>;
}
