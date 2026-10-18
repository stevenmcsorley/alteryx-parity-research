import { useCallback, useEffect, useMemo, useRef } from 'react';
import {
  Background, BackgroundVariant, Controls, MiniMap, ReactFlow, useReactFlow, type Connection, type Edge,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useEditor } from '../state/store';
import { nodeTypes } from './nodes';
import { categoryColor } from '../ui/categories';
import { notify } from '../ui/toast';

export const TOOL_MIME = 'application/x-flowbench-tool';

export function Canvas() {
  const nodes = useEditor((s) => s.nodes);
  const renderedNodes = useMemo(() => {
    const byId = new Map(nodes.map((node) => [node.id, node]));
    return nodes.map((node) => {
      let parent = node.parentId ? byId.get(node.parentId) : undefined;
      let hidden = false;
      while (parent) {
        if (parent.data.kind === 'container' && parent.data.collapsed) { hidden = true; break; }
        parent = parent.parentId ? byId.get(parent.parentId) : undefined;
      }
      if (hidden) return { ...node, hidden: true };
      if (node.data.kind === 'container' && node.data.collapsed)
        return { ...node, style: { ...node.style, height: 48 } };
      return node;
    });
  }, [nodes]);
  const edges = useEditor((s) => s.edges);
  const onNodesChange = useEditor((s) => s.onNodesChange);
  const onEdgesChange = useEditor((s) => s.onEdgesChange);
  const connect = useEditor((s) => s.connect);
  const addTool = useEditor((s) => s.addTool);
  const addApplicationAction = useEditor((s) => s.addApplicationAction);
  const addApplicationCondition = useEditor((s) => s.addApplicationCondition);
  const addApplicationError = useEditor((s) => s.addApplicationError);
  const addComment = useEditor((s) => s.addComment);
  const nodeRows = useEditor((s) => s.run.nodeRows);
  const runStatus = useEditor((s) => s.run.status);
  const tools = useEditor((s) => s.tools);
  const catalog = useEditor((s) => s.catalog);
  const flow = useReactFlow();
  const wrapper = useRef<HTMLDivElement>(null);
  const loadVersion = useEditor((s) => s.loadVersion);
  useEffect(() => {
    if (!loadVersion) return;
    const t = setTimeout(() => flow.fitView({ padding: 0.15, maxZoom: 1.1, duration: 0 }), 30);
    return () => clearTimeout(t);
  }, [loadVersion, flow]);

  const decoratedEdges: Edge[] = useMemo(() => edges.map((e) => {
    const rows = nodeRows[e.source]?.[e.sourceHandle ?? 'output'];
    return {
      ...e,
      animated: runStatus === 'running' && rows === undefined,
      label: e.data?.kind === 'application-condition' ? e.label : rows !== undefined ? rows.toLocaleString() : undefined,
      labelShowBg: true,
      labelBgPadding: [5, 1] as [number, number],
      labelBgBorderRadius: 8,
      labelStyle: { fontFamily: 'var(--mono)', fontSize: 10.5, fill: 'var(--accent)' },
      labelBgStyle: { fill: 'var(--panel)', stroke: 'var(--border)' },
      ariaLabel: `Connection ${e.source}.${e.sourceHandle} to ${e.target}.${e.targetHandle}`,
    };
  }), [edges, nodeRows, runStatus]);

  const onConnect = useCallback((c: Connection) => {
    const err = connect(c);
    if (err) notify(err);
  }, [connect]);

  const isValidConnection = useCallback((c: Connection | Edge) => c.source !== c.target, []);

  const onDragOver = useCallback((e: React.DragEvent) => {
    if (e.dataTransfer.types.includes(TOOL_MIME)) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    }
  }, []);

  const onDrop = useCallback((e: React.DragEvent) => {
    const toolId = e.dataTransfer.getData(TOOL_MIME);
    if (!toolId) return;
    e.preventDefault();
    if (toolId === 'flowengine.application_condition') {
      const pos = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
      addApplicationCondition({ x: Math.round(pos.x - 80), y: Math.round(pos.y - 25) });
      return;
    }
    if (toolId === 'flowengine.application_error') {
      const pos = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
      addApplicationError({ x: Math.round(pos.x - 80), y: Math.round(pos.y - 25) });
      return;
    }
    if (toolId === 'flowengine.explorer') {
      const pos = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
      useEditor.getState().addExplorer({ x: Math.round(pos.x - 80), y: Math.round(pos.y - 25) });
      return;
    }
    if (toolId === 'flowengine.comment') {
      const pos = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
      addComment({ x: Math.round(pos.x - 80), y: Math.round(pos.y - 25) });
      return;
    }
    if (toolId === 'flowengine.application_action' || toolId.startsWith('flowengine.application_action:')) {
      const pos = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
      const catalogId = toolId.slice('flowengine.application_action:'.length);
      addApplicationAction({ x: Math.round(pos.x - 80), y: Math.round(pos.y - 25) }, catalogId ? catalogId.split(':').pop() : undefined, catalogId || 'Enter a value');
      return;
    }
    if (!tools[toolId]) {
      notify('This tool has no implementation yet and cannot be placed in a runnable workflow.');
      return;
    }
    const pos = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    addTool(toolId, { x: Math.round(pos.x - 80), y: Math.round(pos.y - 25) });
  }, [flow, tools, addTool, addApplicationAction, addApplicationCondition, addApplicationError, addComment]);

  const minimapColor = useCallback((n: any) => {
    if (n.type !== 'tool') return 'transparent';
    const m = tools[n.data.tool];
    const cat = catalog.find((c) => m?.catalog_ids.includes(c.id))?.category;
    return categoryColor(cat);
  }, [tools, catalog]);

  return (
    <div className="canvas" ref={wrapper} onDragOver={onDragOver} onDrop={onDrop} data-testid="canvas">
      <ReactFlow
        nodes={renderedNodes}
        edges={decoratedEdges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        defaultViewport={{ x: 40, y: 40, zoom: 1 }}
        colorMode="system"
        minZoom={0.15}
        maxZoom={2.5}
        snapToGrid
        snapGrid={[10, 10]}
        selectionOnDrag
        panOnDrag={[1, 2]}
        multiSelectionKeyCode={['Shift', 'Control', 'Meta']}
        deleteKeyCode={null}
        proOptions={{ hideAttribution: true }}
        defaultEdgeOptions={{ style: { strokeWidth: 1.6 } }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} color="var(--grid)" />
        <MiniMap pannable zoomable nodeColor={minimapColor} ariaLabel="Workflow overview"
          bgColor="var(--panel)" maskColor="color-mix(in srgb, var(--text) 8%, transparent)" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
