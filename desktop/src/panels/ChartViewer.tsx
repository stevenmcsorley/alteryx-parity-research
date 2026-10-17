import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { isTauri } from '../engine/transport';
import { useEditor } from '../state/store';
import { promptPath } from '../ui/dialogs';
import { notify } from '../ui/toast';

const MIN_SCALE = 0.1;
const MAX_SCALE = 16;
const STEP = 1.25;

export function svgDataUrl(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/** The chart's <title>, with XML entities decoded, for captions and file names. */
export function chartTitle(svg: string): string {
  const raw = /<title>([^<]*)<\/title>/.exec(svg)?.[1];
  if (!raw) return 'Chart';
  const doc = new DOMParser().parseFromString(`<t>${raw}</t>`, 'text/xml');
  return doc.documentElement.textContent?.trim() || 'Chart';
}

export function chartFileStem(svg: string): string {
  return chartTitle(svg).replace(/[^A-Za-z0-9 _-]+/g, '').trim().replace(/\s+/g, '_') || 'chart';
}

/** Natural size from the SVG's width/height (or viewBox), falling back to the decoded image. */
function svgSize(svg: string): { w: number; h: number } | null {
  const head = /<svg[^>]*>/.exec(svg)?.[0] ?? '';
  const num = (name: string) => Number(new RegExp(`\\s${name}="([0-9.]+)`).exec(head)?.[1]);
  const w = num('width'), h = num('height');
  if (w > 0 && h > 0) return { w, h };
  const vb = /viewBox="[-0-9.]+[ ,]+[-0-9.]+[ ,]+([0-9.]+)[ ,]+([0-9.]+)"/.exec(head);
  return vb ? { w: Number(vb[1]), h: Number(vb[2]) } : null;
}

/** Rasterise the SVG on a white background at `scale` × its natural size; returns base64 PNG data. */
export async function svgToPngBase64(svg: string, scale = 2): Promise<string> {
  const img = new Image();
  img.decoding = 'sync';
  await new Promise<void>((resolve, reject) => {
    img.onload = () => resolve();
    img.onerror = () => reject(new Error('The chart could not be converted to PNG'));
    img.src = svgDataUrl(svg);
  });
  const size = svgSize(svg) ?? { w: img.naturalWidth || 800, h: img.naturalHeight || 600 };
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(size.w * scale);
  canvas.height = Math.round(size.h * scale);
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('The chart could not be converted to PNG');
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL('image/png').split(',')[1];
}

async function imageData(svg: string, kind: 'svg' | 'png') {
  return kind === 'svg' ? svg : svgToPngBase64(svg);
}

function browserDownload(name: string, kind: 'svg' | 'png', data: string) {
  const a = document.createElement('a');
  a.href = kind === 'svg' ? svgDataUrl(data) : `data:image/png;base64,${data}`;
  a.download = name;
  a.click();
}

export async function saveChart(svg: string, kind: 'svg' | 'png'): Promise<void> {
  const name = `${chartFileStem(svg)}.${kind}`;
  try {
    if (!isTauri()) { browserDownload(name, kind, await imageData(svg, kind)); return; }
    const base = useEditor.getState().baseDir;
    const path = await promptPath(`Save chart as ${kind.toUpperCase()}`, base ? `${base}${base.includes('\\') ? '\\' : '/'}${name}` : name, kind);
    if (!path) return;
    const { invoke } = await import('@tauri-apps/api/core');
    notify(`Saved ${await invoke<string>('chart_save', { path, kind, data: await imageData(svg, kind) })}`);
  } catch (e) {
    notify(e instanceof Error ? e.message : String(e));
  }
}

export async function openChartExternally(svg: string): Promise<void> {
  try {
    if (!isTauri()) { window.open(svgDataUrl(svg), '_blank', 'noopener'); return; }
    const { invoke } = await import('@tauri-apps/api/core');
    await invoke('chart_open', { name: chartFileStem(svg), kind: 'png', data: await svgToPngBase64(svg) });
  } catch (e) {
    notify(e instanceof Error ? e.message : String(e));
  }
}

/** Full-window chart viewer: fit to the window, zoom with buttons, keys or the wheel, drag to pan. */
export function ChartViewer({ svg, onClose }: { svg: string; onClose: () => void }) {
  const stage = useRef<HTMLDivElement>(null);
  const natural = useRef(svgSize(svg) ?? { w: 800, h: 600 });
  const [view, setView] = useState({ scale: 1, x: 0, y: 0, fit: true });
  const drag = useRef<{ px: number; py: number; x: number; y: number } | null>(null);
  const title = chartTitle(svg);

  const fitView = useCallback(() => {
    const el = stage.current;
    if (!el) return;
    const { w, h } = natural.current;
    const pad = 24;
    const scale = Math.max(MIN_SCALE, Math.min((el.clientWidth - pad) / w, (el.clientHeight - pad) / h, MAX_SCALE));
    setView({ scale, x: (el.clientWidth - w * scale) / 2, y: (el.clientHeight - h * scale) / 2, fit: true });
  }, []);

  /** Zoom about a point in stage coordinates (the centre by default). */
  const zoomTo = useCallback((next: number, cx?: number, cy?: number) => {
    const el = stage.current;
    setView((v) => {
      const scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next));
      const px = cx ?? (el ? el.clientWidth / 2 : 0), py = cy ?? (el ? el.clientHeight / 2 : 0);
      const k = scale / v.scale;
      return { scale, x: px - (px - v.x) * k, y: py - (py - v.y) * k, fit: false };
    });
  }, []);

  useLayoutEffect(() => { fitView(); }, [fitView]);
  useEffect(() => {
    const el = stage.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => setView((v) => { if (v.fit) queueMicrotask(fitView); return v; }));
    ro.observe(el);
    return () => ro.disconnect();
  }, [fitView]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
      else if (e.key === '+' || e.key === '=') zoomTo(view.scale * STEP);
      else if (e.key === '-' || e.key === '_') zoomTo(view.scale / STEP);
      else if (e.key === '0') fitView();
      else if (e.key === '1') zoomTo(1);
      else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, zoomTo, fitView, view.scale]);
  useEffect(() => {
    const el = stage.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      zoomTo(view.scale * Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [zoomTo, view.scale]);

  const { w, h } = natural.current;
  return (
    <div className="chart-viewer" role="dialog" aria-modal="true" aria-label={`Chart: ${title}`}>
      <div className="chart-viewer-bar">
        <strong title={title}>{title}</strong>
        <div className="chart-viewer-tools">
          <button onClick={() => zoomTo(view.scale / STEP)} aria-label="Zoom out" title="Zoom out (−)">−</button>
          <span className="chart-zoom" aria-live="polite">{Math.round(view.scale * 100)}%</span>
          <button onClick={() => zoomTo(view.scale * STEP)} aria-label="Zoom in" title="Zoom in (+)">+</button>
          <button onClick={fitView} title="Fit to window (0)">Fit</button>
          <button onClick={() => zoomTo(1)} title="Actual size (1)">100%</button>
          <span className="sep" />
          <button onClick={() => void saveChart(svg, 'png')}>Save PNG</button>
          <button onClick={() => void saveChart(svg, 'svg')}>Save SVG</button>
          <button onClick={() => void openChartExternally(svg)} title="Open in the default image viewer">Open</button>
          <button onClick={onClose} aria-label="Close chart" title="Close (Esc)">✕</button>
        </div>
      </div>
      <div ref={stage} className="chart-viewer-stage" data-testid="chart-stage"
        onPointerDown={(e) => { drag.current = { px: e.clientX, py: e.clientY, x: view.x, y: view.y }; (e.target as Element).setPointerCapture?.(e.pointerId); }}
        onPointerMove={(e) => { const d = drag.current; if (d) setView((v) => ({ ...v, x: d.x + e.clientX - d.px, y: d.y + e.clientY - d.py, fit: false })); }}
        onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}
        onDoubleClick={(e) => { if (view.fit) { const r = stage.current!.getBoundingClientRect(); zoomTo(view.scale * 2, e.clientX - r.left, e.clientY - r.top); } else fitView(); }}>
        <img src={svgDataUrl(svg)} alt={title} draggable={false}
          style={{ position: 'absolute', left: view.x, top: view.y, width: w * view.scale, height: h * view.scale }} />
      </div>
    </div>
  );
}
