// Engine transports. The UI never imports engine internals; it speaks JSON-RPC 2.0
// (flowengine/service.py, protocol 2) through one of these.

export type Notification = { method: string; params: any };
export type NotificationHandler = (n: Notification) => void;

export interface EngineTransport {
  readonly kind: 'tauri' | 'http';
  request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
  subscribe(handler: NotificationHandler): () => void;
}

export class EngineRpcError extends Error {
  constructor(message: string, public code: number, public data?: { code?: string; message?: string; node_id?: string | null }) {
    super(message);
  }
}

function unwrap<T>(resp: any): T {
  if (resp && resp.error) throw new EngineRpcError(resp.error.message, resp.error.code, resp.error.data);
  return resp?.result as T;
}

/** Browser/dev transport: loopback HTTP POST /rpc + Server-Sent Events /events. */
export class HttpTransport implements EngineTransport {
  readonly kind = 'http' as const;
  private nextId = 1;
  private handlers = new Set<NotificationHandler>();
  private source: EventSource | null = null;

  constructor(private baseUrl: string) {}

  async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    const id = this.nextId++;
    const res = await fetch(`${this.baseUrl}/rpc`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id, method, params }),
    });
    if (!res.ok) throw new EngineRpcError(`Engine HTTP ${res.status}`, -32000);
    return unwrap<T>(await res.json());
  }

  subscribe(handler: NotificationHandler): () => void {
    this.handlers.add(handler);
    if (!this.source && typeof EventSource !== 'undefined') {
      this.source = new EventSource(`${this.baseUrl}/events`);
      this.source.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          this.handlers.forEach((h) => h({ method: msg.method, params: msg.params }));
        } catch {
          /* ignore malformed frames */
        }
      };
    }
    return () => {
      this.handlers.delete(handler);
    };
  }
}

/** Desktop transport: Tauri command `engine_request` + event `engine-notification`. */
export class TauriTransport implements EngineTransport {
  readonly kind = 'tauri' as const;

  async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    const { invoke } = await import('@tauri-apps/api/core');
    const resp = await invoke<any>('engine_request', { method, params });
    return unwrap<T>(resp);
  }

  subscribe(handler: NotificationHandler): () => void {
    let unlisten: (() => void) | null = null;
    let cancelled = false;
    import('@tauri-apps/api/event').then(({ listen }) =>
      listen<any>('engine-notification', (e) => handler({ method: e.payload.method, params: e.payload.params })).then((u) => {
        if (cancelled) u();
        else unlisten = u;
      }),
    );
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }
}

export function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

export function createTransport(): EngineTransport {
  if (isTauri()) return new TauriTransport();
  const url = (import.meta as any).env?.VITE_ENGINE_URL || 'http://127.0.0.1:8765';
  return new HttpTransport(url);
}
