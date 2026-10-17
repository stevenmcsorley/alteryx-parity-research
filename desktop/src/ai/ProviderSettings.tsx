import { useEffect, useState } from 'react';

export type LocalProvider = { kind: 'ollama' | 'openai'; base_url: string; context_tokens: number; loopback: boolean };
type Server = 'ollama' | 'lmstudio' | 'openai';

const SERVER_KEY = 'flowbench.ai.localServer';
export const SERVERS: Record<Server, { label: string; kind: 'ollama' | 'openai'; url: string; help: string }> = {
  ollama: { label: 'Ollama', kind: 'ollama', url: 'http://localhost:11434',
    help: 'Install Ollama from ollama.com, then pull a model that supports tool calling, for example: ollama pull qwen2.5:7b' },
  lmstudio: { label: 'LM Studio', kind: 'openai', url: 'http://localhost:1234/v1',
    help: 'In LM Studio, load a model that supports tool use and start the local server (Developer tab). Set the context length when loading the model.' },
  openai: { label: 'Other OpenAI-compatible server', kind: 'openai', url: 'http://localhost:8000/v1',
    help: 'Any server with /v1/chat/completions and tool calling: llama.cpp server, vLLM, Jan or LocalAI.' },
};

export function storedServer(local: LocalProvider | null): Server {
  try {
    const saved = localStorage.getItem(SERVER_KEY) as Server | null;
    if (saved && SERVERS[saved] && (!local || SERVERS[saved].kind === local.kind)) return saved;
  } catch { /* fall through to the server kind */ }
  return local?.kind === 'openai' ? 'lmstudio' : 'ollama';
}

export function providerLabel(local: LocalProvider | null): string {
  return local ? SERVERS[storedServer(local)].label : 'ChatGPT';
}

type Props = {
  local: LocalProvider | null;
  disabled: boolean;
  chatgptAccount: string;
  native: <T>(command: string, args?: Record<string, unknown>) => Promise<T>;
  onChanged: () => Promise<void>;
  onSignIn: () => void;
  onSignOut: () => void;
  chatgptConnected: boolean;
};

export function ProviderSettings({ local, disabled, chatgptAccount, chatgptConnected, native, onChanged, onSignIn, onSignOut }: Props) {
  const [mode, setMode] = useState<'chatgpt' | 'local'>(local ? 'local' : 'chatgpt');
  const [server, setServer] = useState<Server>(storedServer(local));
  const [address, setAddress] = useState(local?.base_url ?? SERVERS[storedServer(local)].url);
  const [context, setContext] = useState(String(local?.context_tokens ?? 32768));
  const [apiKey, setApiKey] = useState('');
  const [working, setWorking] = useState(false);
  const [note, setNote] = useState<{ text: string; ok: boolean } | null>(null);
  // The provider status can arrive after the panel opens.
  useEffect(() => {
    if (!local) return;
    setMode('local'); setServer(storedServer(local)); setAddress(local.base_url); setContext(String(local.context_tokens));
  }, [local?.kind, local?.base_url, local?.context_tokens]);

  function chooseServer(next: Server) {
    setServer(next);
    if (Object.values(SERVERS).some((s) => s.url === address) || !address) setAddress(SERVERS[next].url);
    setNote(null);
  }
  async function connectLocal() {
    setWorking(true); setNote(null);
    try {
      const models = await native<unknown[]>('ai_set_provider', { provider: 'local', kind: SERVERS[server].kind, baseUrl: address,
        contextTokens: Number(context) || undefined, apiKey: apiKey || undefined });
      try { localStorage.setItem(SERVER_KEY, server); } catch { /* label only */ }
      setNote(models.length
        ? { text: `Connected · ${models.length} model${models.length === 1 ? '' : 's'} available`, ok: true }
        : { text: 'Connected, but the server lists no chat models. Download or load one, then reconnect.', ok: false });
      await onChanged();
    } catch (e) { setNote({ text: e instanceof Error ? e.message : String(e), ok: false }); }
    finally { setWorking(false); }
  }
  async function useChatgpt() {
    setMode('chatgpt');
    if (!local) return;
    setWorking(true); setNote(null);
    try { await native('ai_set_provider', { provider: 'chatgpt' }); await onChanged(); }
    catch (e) { setNote({ text: e instanceof Error ? e.message : String(e), ok: false }); }
    finally { setWorking(false); }
  }

  const busy = disabled || working;
  return <section className="ai-settings-section" aria-label="Model provider">
    <h3>Model</h3>
    <div className="ai-segmented" role="radiogroup" aria-label="Model provider">
      <button role="radio" aria-checked={mode === 'chatgpt'} disabled={busy} onClick={() => void useChatgpt()}>ChatGPT</button>
      <button role="radio" aria-checked={mode === 'local'} disabled={busy} onClick={() => setMode('local')}>Local model</button>
    </div>
    {mode === 'chatgpt' ? <div className="ai-settings-body">
      <p className="ai-menu-note">Uses your ChatGPT plan. Prompts and the workflow data the AI asks for are sent to OpenAI.</p>
      {chatgptConnected
        ? <div className="ai-settings-row"><span>{chatgptAccount || 'Connected'}</span><button onClick={onSignOut} disabled={busy}>Disconnect</button></div>
        : <button className="primary" onClick={onSignIn} disabled={busy}>Continue with ChatGPT</button>}
    </div> : <div className="ai-settings-body">
      <p className="ai-menu-note">Runs on a model server you control. Nothing leaves this computer when the server is on localhost.</p>
      <label>Server
        <select value={server} onChange={(e) => chooseServer(e.target.value as Server)} disabled={busy}>
          {(Object.keys(SERVERS) as Server[]).map((key) => <option key={key} value={key}>{SERVERS[key].label}</option>)}
        </select></label>
      <label>Address<input value={address} onChange={(e) => setAddress(e.target.value)} disabled={busy} spellCheck={false} /></label>
      {SERVERS[server].kind === 'ollama' && <label>Context length (tokens)
        <input type="number" min={2048} step={1024} value={context} onChange={(e) => setContext(e.target.value)} disabled={busy} />
        <small className="ai-menu-note">Flowbench’s tool descriptions alone take about 6k tokens; 32k or more works best. Larger values use more memory.</small></label>}
      {server === 'openai' && <label>API key (optional)
        <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} disabled={busy} autoComplete="off" />
        <small className="ai-menu-note">Kept in memory until Flowbench closes.</small></label>}
      <small className="ai-menu-note">{SERVERS[server].help}</small>
      <button className="primary" onClick={() => void connectLocal()} disabled={busy || !address.trim()}>{working ? 'Connecting…' : local ? 'Reconnect' : 'Connect'}</button>
    </div>}
    {note && <p className={`ai-settings-note ${note.ok ? 'ok' : 'err'}`} role={note.ok ? 'status' : 'alert'}>{note.text}</p>}
  </section>;
}
