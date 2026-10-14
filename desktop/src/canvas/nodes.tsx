import { memo } from 'react';
import { Handle, NodeResizer, Position, type NodeProps } from '@xyflow/react';
import { useEditor } from '../state/store';
import type { ApplicationActionNodeData, CanvasNode, CommentNodeData, ContainerNodeData, ToolNodeData } from '../model/workflow';
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
          <div className="port in" title="Configuration action target">
            Action<Handle type="target" position={Position.Left} id="__action__" className="multiple"
              style={{ top: '50%' }} data-testid={`in-${id}-__action__`} />
          </div>
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

export const ApplicationActionNode = memo(function ApplicationActionNode({ id, data, selected }: NodeProps<CanvasNode>) {
  const d = data as ApplicationActionNodeData;
  const connected = useEditor((s) => s.edges.some((e) => e.source === id && e.data?.kind === 'application-action'));
  return <div className={`tool-node application-action-node ${selected ? 'selected' : ''}`} data-testid={`node-${id}`}>
    <div className="head" style={{ background: 'color-mix(in srgb, #805ad5 13%, transparent)' }}>
      <div className="glyph" style={{ background: '#805ad5' }} aria-hidden>A</div>
      <div className="names"><div className="tname">Flowbench Action</div><div className="nid">{id}</div></div>
    </div>
    <div className="ports" style={{ minHeight: 44 }}>
      <div className="port in" title="Question is supplied by the analytic application form">
        Question <span className="count">{d.question.type}</span>
      </div>
      <div className="port out">Action
        <Handle type="source" position={Position.Right} id="Action" style={{ top: '50%' }} data-testid={`out-${id}-Action`} />
      </div>
    </div>
    <div className="annot">{connected ? `Updates ${d.config_path.join('.')}` : 'Connect Action to a tool target'}</div>
  </div>;
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

export const nodeTypes = { tool: ToolNode, applicationAction: ApplicationActionNode, comment: CommentNode, container: ContainerNode };
