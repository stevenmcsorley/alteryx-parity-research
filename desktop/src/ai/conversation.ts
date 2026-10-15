import { create } from 'zustand';

export type ChatMessage = {
  id: string;
  turnId: string;
  role: 'user' | 'assistant';
  text: string;
  state: 'streaming' | 'completed' | 'failed' | 'cancelled';
  error?: string;
  activity?: string[];
};
export type TurnState = 'idle' | 'submitting' | 'streaming' | 'tool-running' | 'completed' | 'failed' | 'cancelled';
type ConversationState = {
  messages: ChatMessage[];
  status: TurnState;
  turnId: string | null;
  assistantMessageId: string | null;
  cycleId: string | null;
  roundPrefix: string;
  setStatus(status: TurnState): void;
  begin(user: ChatMessage, assistant: ChatMessage): void;
  responseStart(turnId: string, cycleId: string): void;
  delta(turnId: string, messageId: string, cycleId: string, text: string): void;
  authoritative(turnId: string, messageId: string, cycleId: string, text: string): void;
  activity(turnId: string, label: string): void;
  finish(turnId: string, status: 'completed' | 'failed' | 'cancelled', error?: string): void;
  reset(): void;
};

export const useAiConversation = create<ConversationState>((set) => ({
  messages: [], status: 'idle', turnId: null, assistantMessageId: null, cycleId: null, roundPrefix: '',
  setStatus: (status) => set({ status }),
  begin: (user, assistant) => set((s) => ({ messages: [...s.messages, user, assistant], status: 'submitting', turnId: user.turnId, assistantMessageId: assistant.id, cycleId: null, roundPrefix: '' })),
  responseStart: (turnId, cycleId) => set((s) => s.turnId === turnId ? { status: 'streaming', cycleId, roundPrefix: s.messages.find((m) => m.id === s.assistantMessageId)?.text ?? '' } : s),
  delta: (turnId, messageId, cycleId, text) => set((s) => {
    if (s.turnId !== turnId || s.assistantMessageId !== messageId || s.cycleId !== cycleId) return s;
    return { messages: s.messages.map((m) => m.id === messageId ? { ...m, text: m.text + text, state: 'streaming' } : m), status: 'streaming' };
  }),
  authoritative: (turnId, messageId, cycleId, text) => set((s) => {
    if (s.turnId !== turnId || s.assistantMessageId !== messageId || s.cycleId !== cycleId) return s;
    return { cycleId: null, messages: s.messages.map((m) => m.id === messageId ? { ...m, text: s.roundPrefix + text, state: 'streaming' } : m) };
  }),
  activity: (turnId, label) => set((s) => {
    if (s.turnId !== turnId || !s.assistantMessageId) return s;
    return { status: 'tool-running', messages: s.messages.map((m) => m.id === s.assistantMessageId ? { ...m, activity: [...(m.activity ?? []), label] } : m) };
  }),
  finish: (turnId, status, error) => set((s) => {
    if (s.turnId !== turnId || !s.assistantMessageId) return s;
    // Do not retain empty assistant bubbles when a failed/cancelled turn had no text.
    const messages = s.messages.flatMap((m) => m.id === s.assistantMessageId
      ? (status === 'completed' && !m.text.trim() ? [] : [{ ...m, state: status, ...(error ? { error } : {}) }])
      : [m]);
    return { messages, status, turnId: null, assistantMessageId: null, cycleId: null, roundPrefix: '' };
  }),
  reset: () => set({ messages: [], status: 'idle', turnId: null, assistantMessageId: null, cycleId: null, roundPrefix: '' }),
}));

let listener: Promise<() => void> | undefined;
/** Install once for the lifetime of the desktop WebView; tab changes never detach it. */
export function ensureAiStreamListener(): Promise<() => void> {
  if (!listener) listener = import('@tauri-apps/api/event').then(({ listen }) => listen<{
    type: 'delta' | 'completed' | 'cancelled'; turn_id: string; message_id: string; cycle_id: string; text?: string;
  }>('ai-stream', ({ payload }) => {
    if (payload.type === 'delta') useAiConversation.getState().delta(payload.turn_id, payload.message_id, payload.cycle_id, payload.text ?? '');
  })).catch((error) => { listener = undefined; throw error; });
  return listener;
}

export function resetListenerForTests() { listener = undefined; }
