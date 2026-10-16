import { beforeEach, describe, expect, it } from 'vitest';
import { useAiConversation } from './conversation';

beforeEach(() => useAiConversation.getState().reset());
function begin() {
  useAiConversation.getState().begin(
    { id: 'user_turn_1', turnId: 'turn_1', role: 'user', text: 'hello', state: 'completed' },
    { id: 'assistant_turn_1', turnId: 'turn_1', role: 'assistant', text: '', state: 'streaming' },
  );
}
const last = <T,>(items: T[]) => items[items.length - 1];

describe('conversation turn state', () => {
  it('reconciles stream deltas with the final response and ignores late events', () => {
    begin();
    useAiConversation.getState().responseStart('turn_1', 'cycle_1');
    useAiConversation.getState().delta('turn_1', 'assistant_turn_1', 'cycle_1', 'Hel');
    useAiConversation.getState().delta('turn_1', 'assistant_turn_1', 'cycle_1', 'lo');
    useAiConversation.getState().authoritative('turn_1', 'assistant_turn_1', 'cycle_1', 'Hello');
    useAiConversation.getState().delta('turn_1', 'assistant_turn_1', 'cycle_1', ' late duplicate');
    expect(last(useAiConversation.getState().messages)?.text).toBe('Hello');
  });

  it('recovers completed output when no text event was delivered', () => {
    begin();
    useAiConversation.getState().responseStart('turn_1', 'cycle_1');
    useAiConversation.getState().authoritative('turn_1', 'assistant_turn_1', 'cycle_1', 'Recovered final answer');
    useAiConversation.getState().finish('turn_1', 'completed');
    expect(last(useAiConversation.getState().messages)).toMatchObject({ text: 'Recovered final answer', state: 'completed' });
  });

  it('retains previous tool-cycle text while reconciling the next assistant response', () => {
    begin();
    useAiConversation.getState().responseStart('turn_1', 'cycle_1');
    useAiConversation.getState().authoritative('turn_1', 'assistant_turn_1', 'cycle_1', 'Inspecting.');
    useAiConversation.getState().responseStart('turn_1', 'cycle_2');
    useAiConversation.getState().delta('turn_1', 'assistant_turn_1', 'cycle_1', 'late old cycle');
    useAiConversation.getState().authoritative('turn_1', 'assistant_turn_1', 'cycle_2', 'Done.');
    expect(last(useAiConversation.getState().messages)?.text).toBe('Inspecting.Done.');
  });

  it('ignores events for a different request and carries failure on the assistant message', () => {
    begin();
    useAiConversation.getState().responseStart('turn_1', 'cycle_1');
    useAiConversation.getState().delta('turn_other', 'assistant_turn_1', 'cycle_1', 'ignored');
    useAiConversation.getState().finish('turn_1', 'failed', 'Network interrupted');
    expect(last(useAiConversation.getState().messages)).toMatchObject({ text: '', state: 'failed', error: 'Network interrupted' });
  });

  it('removes a blank completed assistant bubble', () => {
    begin();
    useAiConversation.getState().finish('turn_1', 'completed');
    expect(useAiConversation.getState().messages.some((message) => message.role === 'assistant')).toBe(false);
  });

  it('discards only interrupted-cycle text and ignores its late events during recovery', () => {
    begin();
    const state = () => useAiConversation.getState();
    state().responseStart('turn_1', 'completed_cycle');
    state().authoritative('turn_1', 'assistant_turn_1', 'completed_cycle', 'Changes applied.');
    state().responseStart('turn_1', 'broken_cycle');
    state().delta('turn_1', 'assistant_turn_1', 'broken_cycle', 'Unfinished');
    state().discardResponse('turn_other', 'broken_cycle');
    expect(last(state().messages)?.text).toBe('Changes applied.Unfinished');
    state().discardResponse('turn_1', 'broken_cycle');
    state().delta('turn_1', 'assistant_turn_1', 'broken_cycle', 'late');
    state().responseStart('turn_1', 'retry_cycle');
    state().delta('turn_1', 'assistant_turn_1', 'broken_cycle', 'late again');
    state().authoritative('turn_1', 'assistant_turn_1', 'retry_cycle', 'Verified.');
    expect(last(state().messages)?.text).toBe('Changes applied.Verified.');
  });
});
