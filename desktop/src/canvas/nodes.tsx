import { memo } from 'react';
import { Handle, NodeResizer, Position, type NodeProps } from '@xyflow/react';
import { useEditor } from '../state/store';
import type { CanvasNode, CommentNodeData, ContainerNodeData, ToolNodeData } from '../model/workflow';
import { categoryColor, glyph } from '../ui/categories';

const PORT_H = 20;

function anchorName(name: string) {
  return name.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

export const ToolNode = memo(function ToolNode({ id, data, selected }: NodeProps<CanvasNode>) {
  const d = data as ToolNodeData;
  const manifest = useEditor((s) => s.tools[d.tool]);
  const category = useEditor((s) => s.catalog.find((c) => manifest?.catalog_ids.includes(c.id))?.category);
  const status = useEditor((s) => s.run.nodeStatus[id]);
  const rows = useEditor((s) => s.run.nodeRows[id]);
  const problems = useEditor((s) => s.problems.filter((p) => p.node_id === id));
  const inputs = manifest?.inputs ?? [];
  const outputs = manifest?.outputs ?? [];
  const portCount = Math.max(inputs.length, outputs.length, 1);
  const name = manifest?.display_name ?? d.tool;
  const color = categoryColor(category);
  const cls = ['tool-node', selected && 'selected', d.disabled && 'disabled', problems.length && 'has-problem']
    .filter(Boolean).join(' ');
  return (
    <div className={cls} data-testid={`node-${id}`} title={problems.map((p) => p.message).join('\n') || undefined}>
      <div className="head" style={{ background: `color-mix(in srgb, ${color} 10%, transparent)` }}>
        <div className="glyph" style={{ background: color }} aria-hidden>{glyph(name)}</div>
        <div className="names">
          <div className="tname">{d.label || name}</div>
          <div className="nid">{id}</div>
        </div>
        {status && <span className={`run-dot ${status}`} role="img" aria-label={`status ${status}`} />}
        {!manifest && <span className="badge unavailable">unavailable</span>}
      </div>
      <div className="ports" style={{ minHeight: portCount * PORT_H + 4 }}>
        <div>
          {inputs.map((a) => (
            <div key={a.name} className="port in" title={a.description || a.name}>
              <Handle type="target" position={Position.Left} id={a.name} className={a.multiple ? 'multiple' : ''}
                style={{ top: '50%' }} data-testid={`in-${id}-${a.name}`} />
              {anchorName(a.name)}{a.multiple ? ' …' : ''}{!a.required && <span style={{ color: 'var(--text-3)' }}> (opt)</span>}
            </div>
          ))}
        </div>
        <div>
          {outputs.map((a) => (
            <div key={a.name} className="port out" title={a.description || a.name}>
              {anchorName(a.name)}
              {rows && rows[a.name] !== undefined && <span className="count" data-testid={`rows-${id}-${a.name}`}>{rows[a.name].toLocaleString()}</span>}
              <Handle type="source" position={Position.Right} id={a.name} style={{ top: '50%' }} data-testid={`out-${id}-${a.name}`} />
            </div>
          ))}
        </div>
      </div>
      {d.annotation && <div className="annot">{d.annotation}</div>}
    </div>
  );
});

export const CommentNode = memo(function CommentNode({ id, data, selected }: NodeProps<CanvasNode>) {
  const d = data as CommentNodeData;
  const update = useEditor((s) => s.updateNodeData);
  return (
    <div className="comment-node" data-testid={`comment-${id}`}>
      <NodeResizer isVisible={!!selected} minWidth={120} minHeight={40} />
      <textarea aria-label="Comment text" defaultValue={d.text} className="nodrag"
        onBlur={(e) => e.target.value !== d.text && update(id, { text: e.target.value })} />
    </div>
  );
});

export const ContainerNode = memo(function ContainerNode({ id, data, selected }: NodeProps<CanvasNode>) {
  const d = data as ContainerNodeData;
  return (
    <div className={`container-node ${d.disabled ? 'disabled' : ''}`} data-testid={`container-${id}`}>
      <NodeResizer isVisible={!!selected} minWidth={160} minHeight={80} />
      <div className="cap">{d.caption}{d.disabled && <span className="badge unavailable">disabled</span>}</div>
    </div>
  );
});

export const nodeTypes = { tool: ToolNode, comment: CommentNode, container: ContainerNode };
