// jsdom lacks a few browser APIs used by React Flow.
class RO {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as any).ResizeObserver = (globalThis as any).ResizeObserver ?? RO;
if (!(globalThis as any).structuredClone) (globalThis as any).structuredClone = (v: unknown) => JSON.parse(JSON.stringify(v));
