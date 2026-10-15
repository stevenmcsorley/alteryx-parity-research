import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { Canvas } from './canvas/Canvas';
import { Palette } from './panels/Palette';
import { Inspector } from './panels/Inspector';
import { ResultsPanel } from './panels/ResultsPanel';
import { Toolbar, doNew, doOpen, doSave } from './panels/Toolbar';
import { Toasts } from './ui/toast';
import { DialogHost } from './ui/dialogs';
import { useEditor } from './state/store';
import { cancelRun, initEngine, runWorkflow, scheduleValidate } from './state/engineActions';
import { AiSidebar } from './ai/AiSidebar';

function RightDock({ aiOpen, selectAi, selectInspector, onResize, onResizeKey }: { aiOpen: boolean; selectAi: () => void; selectInspector: () => void; onResize: (event: ReactPointerEvent) => void; onResizeKey: (event: ReactKeyboardEvent) => void }) {
  return <section className="app-right right-dock" aria-label="Inspector and AI workspace">
    <div className="dock-resizer" role="separator" aria-orientation="vertical" aria-label="Resize right workspace" tabIndex={0} onPointerDown={onResize} onKeyDown={onResizeKey} />
    <div className="right-tabs" role="tablist" aria-label="Right workspace panels">
      <button role="tab" aria-selected={!aiOpen} onClick={selectInspector}>Inspector</button>
      <button role="tab" aria-selected={aiOpen} onClick={selectAi}>Flowbench AI</button>
    </div>
    <div className="right-panel" role="tabpanel" aria-label={aiOpen ? 'Flowbench AI' : 'Inspector'}>
      {aiOpen ? <AiSidebar embedded close={selectInspector} /> : <Inspector embedded />}
    </div>
  </section>;
}

function isTyping(e: KeyboardEvent) {
  const t = e.target as HTMLElement | null;
  return !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
}

export function useShortcuts() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const s = useEditor.getState();
      const mod = e.ctrlKey || e.metaKey;
      if (e.key === 'F5') { e.preventDefault(); void runWorkflow(); return; }
      if (mod && e.key.toLowerCase() === 's') { e.preventDefault(); void doSave(e.shiftKey); return; }
      if (mod && e.key.toLowerCase() === 'o') { e.preventDefault(); void doOpen(); return; }
      if (mod && e.key.toLowerCase() === 'n') { e.preventDefault(); doNew(); return; }
      if (isTyping(e)) return;
      if (e.key === 'Escape' && s.run.status === 'running') { void cancelRun(); return; }
      if (mod && e.key.toLowerCase() === 'z' && !e.shiftKey) { e.preventDefault(); s.undo(); return; }
      if (mod && (e.key.toLowerCase() === 'y' || (e.key.toLowerCase() === 'z' && e.shiftKey))) { e.preventDefault(); s.redo(); return; }
      if (mod && e.key.toLowerCase() === 'a') { e.preventDefault(); s.selectAll(); return; }
      if (mod && e.key.toLowerCase() === 'c') { s.copySelected(); return; }
      if (mod && e.key.toLowerCase() === 'v') { s.paste(); return; }
      if (mod && e.key.toLowerCase() === 'd') { e.preventDefault(); s.duplicateSelected(); return; }
      if (mod && e.key.toLowerCase() === 'g') { e.preventDefault(); s.groupSelected(); return; }
      if (e.key === 'Delete' || e.key === 'Backspace') { s.deleteSelected(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
}

/** Re-validate whenever the document (not selection/run state) changes. */
function useAutoValidate() {
  useEffect(() => {
    let prev = useEditor.getState();
    return useEditor.subscribe((s) => {
      const changed = s.edges !== prev.edges || s.profile !== prev.profile || s.baseDir !== prev.baseDir ||
        s.engineInfo !== prev.engineInfo ||
        (s.nodes !== prev.nodes && (s.nodes.length !== prev.nodes.length ||
          s.nodes.some((n, i) => n.data !== prev.nodes[i]?.data || n.parentId !== prev.nodes[i]?.parentId)));
      prev = s;
      if (changed) scheduleValidate();
    });
  }, []);
}

function EngineOffline() {
  const err = useEditor((s) => s.engineError);
  if (!err) return null;
  return (
    <div className="engine-down" role="alert">
      <div className="card">
        <h3 style={{ marginTop: 0 }}>The workflow engine is not reachable</h3>
        <p>{err}</p>
        <p>The desktop build starts the engine automatically. For the browser build run <code>python -m flowengine serve --http 8765 --history .flowbench/history.sqlite</code> from the repository root.</p>
        <button className="primary" onClick={() => initEngine()}>Retry</button>
      </div>
    </div>
  );
}

export default function App() {
  const [aiOpen, setAiOpen] = useState(false);
  const [dockWidth, setDockWidth] = useState(400);
  const resizing = useRef(false);
  const startResize = (event: ReactPointerEvent) => {
    event.preventDefault();
    resizing.current = true;
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    const onMove = (move: PointerEvent) => { if (resizing.current) setDockWidth(Math.max(310, Math.min(600, window.innerWidth - move.clientX))); };
    const onUp = () => { resizing.current = false; window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onUp); };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  };
  useShortcuts();
  useAutoValidate();
  useEffect(() => { void initEngine(); }, []);
  return (
    <ReactFlowProvider>
      <div className="app" style={{ '--right-dock-width': `${dockWidth}px` } as CSSProperties}>
        <Toolbar onToggleAi={() => setAiOpen((open) => !open)} aiOpen={aiOpen} />
        <Palette />
        <div style={{ gridArea: 'canvas', position: 'relative', minHeight: 0, display: 'flex' }}>
          <Canvas />
          <EngineOffline />
        </div>
        <RightDock aiOpen={aiOpen} selectAi={() => setAiOpen(true)} selectInspector={() => setAiOpen(false)} onResize={startResize} onResizeKey={(event) => {
          if (event.key === 'ArrowLeft') setDockWidth((width) => Math.min(600, width + 16));
          if (event.key === 'ArrowRight') setDockWidth((width) => Math.max(310, width - 16));
        }} />
        <ResultsPanel />
      </div>
      <Toasts />
      <DialogHost />
    </ReactFlowProvider>
  );
}
