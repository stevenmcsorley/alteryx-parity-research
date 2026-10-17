import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEditor } from '../state/store';
import { AiSidebar, modelCell, RESULT_CELL_MAX_CHARS } from './AiSidebar';
import { resetListenerForTests, useAiConversation } from './conversation';
import { workflowRevision } from './workflowPatch';
import { setTransport } from '../state/engineActions';

const tauriMock = vi.hoisted(() => ({ invoke: vi.fn(), handler: null as null | ((event: { payload: any }) => void), listenerCalls: 0 }));
vi.mock('@tauri-apps/api/core', () => ({ invoke: tauriMock.invoke }));
vi.mock('@tauri-apps/api/event', () => ({ listen: vi.fn(async (_name: string, handler: (event: { payload: any }) => void) => {
  tauriMock.listenerCalls++; tauriMock.handler = handler; return () => { tauriMock.handler = null; };
}) }));

beforeEach(() => {
  tauriMock.handler = null; tauriMock.listenerCalls = 0; resetListenerForTests(); useAiConversation.getState().reset(); useAiConversation.getState().setFullControl(null);
  tauriMock.invoke.mockReset().mockImplementation(async (command: string) => {
    if (command === 'ai_status') return { connected: false, plan_usage_authorized: false };
    if (command === 'ai_sign_in') return undefined;
    if (command === 'ai_new_conversation' || command === 'ai_finish_turn' || command === 'ai_abort_turn') return undefined;
    return [];
  });
  useEditor.getState().newWorkflow();
  useEditor.setState({ name: 'Fixture workflow', catalog: [{ id: 'preparation:filter', name: 'Filter', category: 'Preparation',
    implementation_status: 'implemented', verification_status: 'partial', kernel: 'core.filter', documentation_url: null,
    external_prerequisites_blocking: false, unavailable_reason: null }] });
});
afterEach(cleanup);

async function ready() {
  tauriMock.invoke.mockImplementation(async (command: string) => {
    if (command === 'ai_status') return { connected: true, plan_usage_authorized: true, subject_hint: 'Account · safe' };
    if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
    if (command === 'ai_set_data_consent') return undefined;
    if (command === 'ai_finish_turn' || command === 'ai_abort_turn' || command === 'ai_new_conversation' || command === 'ai_submit_tool_result') return undefined;
    return [];
  });
  render(<AiSidebar close={() => undefined} />);
  await screen.findByRole('option', { name: 'Available model' });
  fireEvent.click(screen.getByRole('button', { name: 'AI settings' }));
  fireEvent.click(screen.getByRole('checkbox', { name: /Allow prompts, workflow context and requested data results/ }));
  await waitFor(() => expect((screen.getByRole('checkbox', { name: /Allow prompts, workflow context and requested data results/ }) as HTMLInputElement).checked).toBe(true));
  fireEvent.click(screen.getByRole('button', { name: 'AI settings' }));
}

describe('Flowbench AI conversation', () => {
  it('shows an isolated validated graph proposal and commits it as one undoable editor change after approval', async () => {
    const manifest = (tool_id: string, inputs: string[], outputs: string[]) => ({ tool_id, version: '1', display_name: tool_id,
      catalog_ids: [], inputs: inputs.map((name) => ({ name, label: name, required: name === 'input', multiple: false, description: '' })),
      outputs: outputs.map((name) => ({ name, label: name, required: false, multiple: false, description: '' })),
      config_schema: { type: 'object' }, side_effects: 'none', deterministic: true, dependencies: [], description: '' });
    useEditor.getState().setEngineMeta({ tools: {
      'core.input_data': manifest('core.input_data', [], ['output']) as any,
      'core.filter': manifest('core.filter', ['input'], ['true', 'false']) as any,
    } });
    setTransport({ request: async (method: string) => { expect(method).toBe('workflow.validate'); return { valid: true, problems: [], schemas: {} }; }, subscribe: () => () => {} } as any);
    const patch = JSON.stringify({ operations: [
      { op: 'add', ref: 'sales_input', tool_id: 'core.input_data', config: { path: 'sales.csv' }, position: { x: 0, y: 0 } },
      { op: 'add', ref: 'sales_filter', tool_id: 'core.filter', config: { expression: '[Sales] > 100' }, position: { x: 220, y: 0 } },
      { op: 'connect', source_ref: 'sales_input', source_anchor: 'output', target_ref: 'sales_filter', target_anchor: 'input' },
    ] });
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_begin') return { turn_id: 'patch_turn', assistant_message_id: 'patch_msg' };
      if (command === 'ai_respond') {
        if (!tauriMock.invoke.mock.calls.some(([cmd]) => cmd === 'ai_submit_tool_result')) return { turn_id: args?.turnId, message_id: 'patch_msg', cycle_id: args?.cycleId, text: '', calls: [
          { name: 'workflow.propose_patch', call_id: 'patch_call', arguments: JSON.stringify({ patch_json: patch, base_revision: workflowRevision(useEditor.getState()) }) },
        ] };
        return { turn_id: args?.turnId, message_id: 'patch_msg', cycle_id: args?.cycleId, text: 'The approved workflow is on the canvas.', calls: [] };
      }
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'Build this sales flow.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByRole('region', { name: 'Workflow change preview' })).toBeTruthy();
    expect(useEditor.getState().nodes).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Approve changes' }));
    await waitFor(() => expect(useEditor.getState().nodes).toHaveLength(2));
    expect(await screen.findByText('The approved workflow is on the canvas.')).toBeTruthy();
    useEditor.getState().undo();
    expect(useEditor.getState().nodes).toHaveLength(0);
  });

  it('recovers an interrupted response after a Full Control edit without repeating the edit', async () => {
    let interruptions = 0;
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const manifest = (tool_id: string, inputs: string[], outputs: string[]) => ({ tool_id, version: '1', display_name: tool_id,
      catalog_ids: [], inputs: inputs.map((name) => ({ name, label: name, required: name === 'input', multiple: false, description: '' })),
      outputs: outputs.map((name) => ({ name, label: name, required: false, multiple: false, description: '' })),
      config_schema: { type: 'object' }, side_effects: 'none', deterministic: true, dependencies: [], description: '' });
    useEditor.getState().setEngineMeta({ engineInfo: { version: 'test', python: '3.12', protocol: 2 } as any,
      tools: { 'core.input_data': manifest('core.input_data', [], ['output']) as any } });
    setTransport({ request: async () => ({ valid: true, problems: [], schemas: {} }), subscribe: () => () => {} } as any);
    const patch = JSON.stringify({ operations: [
      { op: 'add', ref: 'input', tool_id: 'core.input_data', config: { type_inference: 'flowbench' }, position: { x: 0, y: 0 } },
    ] });
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'AI settings' }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Full Control for this workspace/ }));
    expect(confirm).toHaveBeenCalledOnce();
    expect((screen.getByRole('checkbox', { name: /Full Control for this workspace/ }) as HTMLInputElement).checked).toBe(true);
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_begin') return { turn_id: 'full_turn', assistant_message_id: 'full_msg' };
      if (command === 'ai_respond') {
        if (!tauriMock.invoke.mock.calls.some(([cmd]) => cmd === 'ai_submit_tool_result')) return { turn_id: args?.turnId, message_id: 'full_msg', cycle_id: args?.cycleId, text: '', calls: [
          { name: 'workflow.propose_patch', call_id: 'full_patch', arguments: JSON.stringify({ patch_json: patch, base_revision: workflowRevision(useEditor.getState()) }) },
        ] };
        if (interruptions++ === 0) {
          tauriMock.handler?.({ payload: { type: 'delta', turn_id: args?.turnId, message_id: 'full_msg', cycle_id: args?.cycleId, text: 'Partial discarded answer' } });
          return { status: 'retryable_stream_interruption', turn_id: args?.turnId, message_id: 'full_msg', cycle_id: args?.cycleId };
        }
        return { turn_id: args?.turnId, message_id: 'full_msg', cycle_id: args?.cycleId, text: 'Added the input node.', calls: [] };
      }
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'Create an input node.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await screen.findByText('Added the input node.');
    await waitFor(() => expect(useEditor.getState().nodes).toHaveLength(1));
    expect(screen.queryByRole('region', { name: 'Workflow change preview' })).toBeNull();
    expect(await screen.findByText('Added the input node.')).toBeTruthy();
    expect(screen.queryByText(/Partial discarded answer/)).toBeNull();
    expect(tauriMock.invoke.mock.calls.filter(([cmd]) => cmd === 'ai_begin')).toHaveLength(1);
    expect(tauriMock.invoke.mock.calls.filter(([cmd]) => cmd === 'ai_submit_tool_result')).toHaveLength(1);
    expect(tauriMock.invoke.mock.calls.filter(([cmd]) => cmd === 'ai_respond')).toHaveLength(3);
    expect(tauriMock.invoke.mock.calls.some(([cmd]) => cmd === 'ai_abort_turn')).toBe(false);
    useEditor.getState().undo();
    expect(useEditor.getState().nodes).toHaveLength(0);
    confirm.mockRestore();
  });

  it('keeps connection and privacy settings out of the conversation body', async () => {
    render(<AiSidebar close={() => undefined} />);
    expect(await screen.findByText('What would you like to analyse?')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'AI settings' })).toBeTruthy();
    expect(screen.queryByLabelText('Local read-only agent actions')).toBeNull();
  });

  it('renders authoritative completed text for ten successive responses without relying on deltas', async () => {
    let call = 0;
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_set_data_consent' || command === 'ai_finish_turn') return undefined;
      if (command === 'ai_begin') {
        expect(tauriMock.listenerCalls).toBeGreaterThan(0);
        return { turn_id: `turn_${call + 1}`, assistant_message_id: `msg_${call + 1}` };
      }
      if (command === 'ai_respond') {
        const next = call + 1;
        tauriMock.handler?.({ payload: { type: 'delta', turn_id: args?.turnId, message_id: `msg_${next}`, cycle_id: args?.cycleId, text: `Answer ${next}` } });
        return { turn_id: args?.turnId, message_id: `msg_${next}`, cycle_id: args?.cycleId, text: `Answer ${++call}`, calls: [] };
      }
      if (command === 'ai_new_conversation') return undefined;
      return undefined;
    });
    render(<AiSidebar close={() => undefined} />);
    await screen.findByRole('option', { name: 'Available model' });
    fireEvent.click(screen.getByRole('button', { name: 'AI settings' }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Allow prompts, workflow context and requested data results/ }));
    await waitFor(() => expect((screen.getByRole('checkbox', { name: /Allow prompts, workflow context and requested data results/ }) as HTMLInputElement).checked).toBe(true));
    const composer = screen.getByLabelText('Message Flowbench AI');
    for (let n = 1; n <= 10; n++) {
      fireEvent.change(composer, { target: { value: 'hello' } });
      fireEvent.click(screen.getByRole('button', { name: 'Send' }));
      expect(await screen.findByText(`Answer ${n}`)).toBeTruthy();
      await waitFor(() => expect(useAiConversation.getState().status).toBe('completed'));
    }
    expect(useAiConversation.getState().messages.filter((m) => m.role === 'assistant')).toHaveLength(10);
    expect(useAiConversation.getState().messages.every((m) => m.text.trim())).toBe(true);
  });

  it('handles several tool-call rounds then reconciles final text and rejects stale events', async () => {
    let n = 0;
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_begin') return { turn_id: 'turn_real', assistant_message_id: 'msg_real' };
      if (command === 'ai_respond') {
        n++;
        if (n === 1) return { turn_id: args?.turnId, message_id: 'msg_real', cycle_id: args?.cycleId, text: '', calls: [
          { name: 'app.get_capabilities', call_id: 'call_1', arguments: '{}' },
          { name: 'workflow.get_snapshot', call_id: 'call_2', arguments: '{}' },
        ] };
        if (n === 2) return { turn_id: args?.turnId, message_id: 'msg_real', cycle_id: args?.cycleId, text: '', calls: [
          { name: 'workflow.validate', call_id: 'call_3', arguments: '{}' },
        ] };
        return { turn_id: args?.turnId, message_id: 'msg_real', cycle_id: args?.cycleId, text: 'The workflow is empty.', calls: [] };
      }
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'What does this workflow contain?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('The workflow is empty.')).toBeTruthy();
    expect(tauriMock.invoke.mock.calls.filter(([cmd]) => cmd === 'ai_submit_tool_result')).toHaveLength(3);
    tauriMock.handler?.({ payload: { type: 'delta', turn_id: 'stale', message_id: 'wrong', cycle_id: 'late', text: 'corruption' } });
    expect(screen.queryByText('corruption')).toBeNull();
  });

  it('shows a specific reasoning-only completion error and lets the user inspect safe diagnostics', async () => {
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_begin') return { turn_id: 'turn_reasoning', assistant_message_id: 'msg_reasoning' };
      if (command === 'ai_respond') return { turn_id: args?.turnId, message_id: 'msg_reasoning', cycle_id: args?.cycleId, text: '', empty_reason: 'reasoning_only', calls: [] };
      if (command === 'ai_get_response_diagnostics') return [{ http_status: 200, terminal_status: 'completed', output: { reasoning_items: 1, message_items: 0 } }];
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'hello' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText(/completed with reasoning output but no user-facing text/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'AI settings' }));
    fireEvent.click(screen.getByRole('button', { name: 'Show safe response diagnostics' }));
    expect(await screen.findByText(/^Completed · /)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Show technical details' }));
    expect(await screen.findByText(/\"terminal_status\": \"completed\"/)).toBeTruthy();
  });

  it('supports an explicit no-tools probe without changing the normal read-only tool mode', async () => {
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_begin') return { turn_id: 'turn_probe', assistant_message_id: 'msg_probe' };
      if (command === 'ai_respond') return { turn_id: args?.turnId, message_id: 'msg_probe', cycle_id: args?.cycleId, text: 'Probe answer', calls: [] };
      return undefined;
    });
    fireEvent.click(screen.getByRole('button', { name: 'AI settings' }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Include Flowbench application tools in requests/ }));
    expect((screen.getByRole('checkbox', { name: /Include Flowbench application tools in requests/ }) as HTMLInputElement).checked).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'AI settings' }));
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'hello' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('Probe answer')).toBeTruthy();
    expect(tauriMock.invoke.mock.calls.find(([command]) => command === 'ai_respond')?.[1]).toMatchObject({ toolsEnabled: false });
  });

  it('defaults to low reasoning effort, sends the chosen level and remembers it', async () => {
    localStorage.removeItem('flowbench.ai.effort');
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_begin') return { turn_id: 'turn_effort', assistant_message_id: 'msg_effort' };
      if (command === 'ai_respond') return { turn_id: args?.turnId, message_id: 'msg_effort', cycle_id: args?.cycleId, text: 'Effort answer', calls: [] };
      return undefined;
    });
    const picker = screen.getByRole('combobox', { name: 'Reasoning effort' }) as HTMLSelectElement;
    expect(picker.value).toBe('low');
    expect([...picker.options].map((o) => o.value)).toEqual(['low', 'medium', 'high']);
    fireEvent.change(picker, { target: { value: 'high' } });
    expect(localStorage.getItem('flowbench.ai.effort')).toBe('high');
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'hello' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('Effort answer')).toBeTruthy();
    expect(tauriMock.invoke.mock.calls.find(([command]) => command === 'ai_respond')?.[1]).toMatchObject({ model: 'model-a', effort: 'high' });
    localStorage.removeItem('flowbench.ai.effort');
  });

  it('keeps three unrelated prompts in one native conversation with separate stable turns', async () => {
    let response = 0;
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_set_data_consent' || command === 'ai_finish_turn') return undefined;
      if (command === 'ai_begin') { response++; return { turn_id: `turn_${response}`, assistant_message_id: `msg_${response}` }; }
      if (command === 'ai_respond') return { turn_id: args?.turnId, message_id: `msg_${response}`, cycle_id: args?.cycleId, text: `Result ${response}`, calls: [] };
      return undefined;
    });
    const prompts = ['Summarize the open workflow', 'Which catalogue tools are implemented?', 'Explain this validation issue'];
    for (const prompt of prompts) {
      fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: prompt } });
      fireEvent.click(screen.getByRole('button', { name: 'Send' }));
      expect(await screen.findByText(`Result ${prompts.indexOf(prompt) + 1}`)).toBeTruthy();
      await waitFor(() => expect(useAiConversation.getState().status).toBe('completed'));
    }
    expect(useAiConversation.getState().messages.filter((message) => message.role === 'user').map((message) => message.text)).toEqual(prompts);
    expect(new Set(useAiConversation.getState().messages.map((message) => message.turnId)).size).toBe(3);
  });

  it('preserves the transcript across sidebar unmount and clears it explicitly', async () => {
    useAiConversation.setState({ messages: [{ id: 'message_1', turnId: 'turn_1', role: 'assistant', text: 'Persistent answer', state: 'completed' }] });
    const first = render(<AiSidebar close={() => undefined} />);
    expect(await screen.findByText('Persistent answer')).toBeTruthy();
    first.unmount();
    render(<AiSidebar close={() => undefined} />);
    expect(await screen.findByText('Persistent answer')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
    await waitFor(() => expect(useAiConversation.getState().messages).toHaveLength(0));
  });

  it('keeps failures inside the relevant assistant message and allows retry', async () => {
    let attempt = 0;
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_set_data_consent' || command === 'ai_finish_turn' || command === 'ai_abort_turn') return undefined;
      if (command === 'ai_begin') return { turn_id: `turn_${attempt + 1}`, assistant_message_id: `msg_${attempt + 1}` };
      if (command === 'ai_respond') { if (attempt++ === 0) throw new Error('Temporary response failure'); return { turn_id: args?.turnId, message_id: 'msg_2', cycle_id: args?.cycleId, text: 'Recovered.', calls: [] }; }
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'retry me' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('Temporary response failure')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Recovered.')).toBeTruthy();
  });

  it('bounds interrupted-stream retries and preserves history for a manual continuation', async () => {
    let turn = 0;
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_begin') { turn++; return { turn_id: `turn_${turn}`, assistant_message_id: `msg_${turn}` }; }
      if (command === 'ai_respond') return { turn_id: args?.turnId, message_id: `msg_${turn}`, cycle_id: args?.cycleId,
        ...(turn === 1 ? { status: 'retryable_stream_interruption', calls: [
          { name: 'workflow.propose_patch', call_id: 'must_not_execute', arguments: '{}' },
        ] } : { text: 'Remaining work verified.', calls: [] }) };
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'Build a difficult workflow.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(useAiConversation.getState().status).toBe('failed'), { timeout: 2500 });
    expect(tauriMock.invoke.mock.calls.filter(([cmd]) => cmd === 'ai_respond')).toHaveLength(3);
    expect(tauriMock.invoke.mock.calls.some(([cmd]) => cmd === 'ai_abort_turn' || cmd === 'ai_submit_tool_result')).toBe(false);
    expect(tauriMock.invoke.mock.calls.some(([cmd]) => cmd === 'ai_finish_turn')).toBe(true);
    expect(useAiConversation.getState().messages.slice(-1)[0]?.retryMode).toBe('continue');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('Remaining work verified.');
    const prompts = tauriMock.invoke.mock.calls.filter(([cmd]) => cmd === 'ai_begin').map(([, args]) => args.prompt);
    expect(prompts).toHaveLength(2);
    expect(prompts[1]).toContain('Continue my interrupted request');
    expect(useAiConversation.getState().messages[0].text).toBe('Build a difficult workflow.');
  });

  it('lets the model correct rejected arguments before dispatching any calls', async () => {
    let responses = 0;
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_begin') return { turn_id: 'turn_correct', assistant_message_id: 'msg_correct' };
      if (command === 'ai_respond') {
        responses++;
        const identity = { turn_id: args?.turnId, message_id: 'msg_correct', cycle_id: args?.cycleId };
        if (responses === 1) {
          tauriMock.handler?.({ payload: { type: 'delta', ...identity, text: 'Unvalidated claim' } });
          return { ...identity, needs_tool_correction: true, calls: [
            { name: 'workflow.request_run', call_id: 'withheld', arguments: '{}' },
          ] };
        }
        if (responses === 2) return { ...identity, text: '', calls: [
          { name: 'app.get_capabilities', call_id: 'corrected', arguments: '{}' },
        ] };
        return { ...identity, text: 'Available tools inspected.', calls: [] };
      }
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'Build expense analysis.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await screen.findByText('Available tools inspected.');
    expect(screen.queryByText('Unvalidated claim')).toBeNull();
    expect(tauriMock.invoke.mock.calls.filter(([cmd]) => cmd === 'ai_begin')).toHaveLength(1);
    const submitted = tauriMock.invoke.mock.calls.filter(([cmd]) => cmd === 'ai_submit_tool_result');
    expect(submitted).toHaveLength(1);
    expect(submitted[0][1].callId).toBe('corrected');
    expect(tauriMock.invoke.mock.calls.some(([cmd]) => cmd === 'ai_abort_turn')).toBe(false);
  });

  it('stops repeated invalid tool arguments with completed conversation history intact', async () => {
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_begin') return { turn_id: 'turn_bad_args', assistant_message_id: 'msg_bad_args' };
      if (command === 'ai_respond') return { turn_id: args?.turnId, message_id: 'msg_bad_args',
        cycle_id: args?.cycleId, needs_tool_correction: true, calls: [] };
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'Correct these arguments.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(useAiConversation.getState().status).toBe('failed'));
    expect(tauriMock.invoke.mock.calls.filter(([cmd]) => cmd === 'ai_respond')).toHaveLength(3);
    expect(tauriMock.invoke.mock.calls.some(([cmd]) => cmd === 'ai_abort_turn' || cmd === 'ai_submit_tool_result')).toBe(false);
    expect(tauriMock.invoke.mock.calls.some(([cmd]) => cmd === 'ai_finish_turn')).toBe(true);
    expect(useAiConversation.getState().messages.slice(-1)[0]?.retryMode).toBe('continue');
  });

  it('can stop during reconnect backoff without starting another response', async () => {
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_begin') return { turn_id: 'turn_backoff', assistant_message_id: 'msg_backoff' };
      if (command === 'ai_respond') return { status: 'retryable_stream_interruption', turn_id: args?.turnId,
        message_id: 'msg_backoff', cycle_id: args?.cycleId };
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'Stop recovery.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(useAiConversation.getState().messages.slice(-1)[0]?.activity?.some((a) => a.includes('reconnecting'))).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: 'Stop response' }));
    await waitFor(() => expect(useAiConversation.getState().status).toBe('cancelled'));
    expect(tauriMock.invoke.mock.calls.filter(([cmd]) => cmd === 'ai_respond')).toHaveLength(1);
  });

  it('shows a native begin failure and preserves the unsent draft', async () => {
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_set_data_consent') return undefined;
      if (command === 'ai_begin') throw new Error('Native conversation unavailable');
      return undefined;
    });
    const composer = screen.getByLabelText('Message Flowbench AI');
    fireEvent.change(composer, { target: { value: 'keep this prompt' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Native conversation unavailable');
    expect((composer as HTMLTextAreaElement).value).toBe('keep this prompt');
  });

  it('cancels an active stream cleanly and re-enables the composer', async () => {
    let rejectResponse: ((error: Error) => void) | undefined;
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_set_data_consent' || command === 'ai_abort_turn' || command === 'ai_cancel') return undefined;
      if (command === 'ai_begin') return { turn_id: 'turn_cancel', assistant_message_id: 'msg_cancel' };
      if (command === 'ai_respond') return new Promise((_resolve, reject) => { rejectResponse = reject; });
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'stop this' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop response' }));
    rejectResponse?.(new Error('Generation cancelled'));
    expect(await screen.findByText('Response stopped')).toBeTruthy();
    await waitFor(() => expect((screen.getByLabelText('Message Flowbench AI') as HTMLTextAreaElement).disabled).toBe(false));
    expect(useAiConversation.getState().status).toBe('cancelled');
  });
});

describe('model-facing result cells', () => {
  it('reduces blobs to a size note and cuts long text', () => {
    expect(modelCell('ab'.repeat(5000), 'blob')).toBe('[blob, 5000 bytes]');
    expect(modelCell('x'.repeat(RESULT_CELL_MAX_CHARS + 10), 'v_wstring')).toBe(`${'x'.repeat(RESULT_CELL_MAX_CHARS)}… [10 more characters]`);
    expect(modelCell('short', 'v_wstring')).toBe('short');
    expect(modelCell(null, 'blob')).toBeNull();
  });
});

describe('local model provider', () => {
  it('connects to a local server, needs no consent on localhost and sends no reasoning effort', async () => {
    let local = false;
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return local
        ? { provider: 'local', local: { kind: 'ollama', base_url: 'http://localhost:11434', context_tokens: 32768, loopback: true }, connected: true, plan_usage_authorized: true, data_routing_allowed: true }
        : { provider: 'chatgpt', connected: false, plan_usage_authorized: false };
      if (command === 'ai_set_provider') { local = true; return [{ slug: 'qwen2.5:7b', display_name: 'qwen2.5:7b' }]; }
      if (command === 'ai_models') return local ? [{ slug: 'qwen2.5:7b', display_name: 'qwen2.5:7b', supported_efforts: [] }] : [];
      if (command === 'ai_begin') return { turn_id: 't_local', assistant_message_id: 'm_local' };
      if (command === 'ai_respond') return { turn_id: args?.turnId, message_id: 'm_local', cycle_id: args?.cycleId, text: 'Local answer', calls: [], usage: { input_tokens: 6400, output_tokens: 12 } };
      return undefined;
    });
    render(<AiSidebar close={() => undefined} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Use a local model' }));
    fireEvent.click(screen.getByRole('radio', { name: 'Local model' }));
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect(await screen.findByRole('option', { name: 'qwen2.5:7b' })).toBeTruthy();
    expect(tauriMock.invoke.mock.calls.find(([c]) => c === 'ai_set_provider')?.[1]).toMatchObject({ provider: 'local', kind: 'ollama', baseUrl: 'http://localhost:11434', contextTokens: 32768 });
    expect(screen.getByText('Ollama', { selector: '.ai-provider-badge' })).toBeTruthy();
    expect(screen.queryByRole('combobox', { name: 'Reasoning effort' })).toBeNull();
    expect(screen.getByText(/Data stays on this computer/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'hello' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('Local answer')).toBeTruthy();
    expect(tauriMock.invoke.mock.calls.find(([c]) => c === 'ai_respond')?.[1]).toMatchObject({ model: 'qwen2.5:7b', effort: null });
    expect(screen.getByText('6.4k tokens this turn')).toBeTruthy();
  });
});
