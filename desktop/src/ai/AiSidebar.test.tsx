import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEditor } from '../state/store';
import { agentSavePath, AiSidebar, modelCell } from './AiSidebar';
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
  localStorage.removeItem('flowbench.ai.localServer');
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
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
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
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
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
    expect(prompts[1]).toContain('Resume the SAME task');
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

  it('pauses planning-only loops as resumable and retries the same research objective', async () => {
    let turn = 0;
    let firstTurnCycles = 0;
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_begin') { turn++; return { turn_id: `turn_${turn}`, assistant_message_id: `msg_${turn}` }; }
      if (command === 'ai_respond') {
        const identity = { turn_id: args?.turnId, message_id: `msg_${turn}`, cycle_id: args?.cycleId };
        if (turn === 1) { firstTurnCycles++; return { ...identity, text: '', calls: [], continue: true }; }
        return { ...identity, text: 'The same investigation is complete.', calls: [] };
      }
      return undefined;
    });
    const objective = 'Investigate whether unusual rainfall precedes local rail disruption.';
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: objective } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText(/paused after repeated internal-only steps/i)).toBeTruthy();
    expect(firstTurnCycles).toBe(6);
    expect(tauriMock.invoke.mock.calls.some(([cmd]) => cmd === 'ai_finish_turn')).toBe(true);
    expect(tauriMock.invoke.mock.calls.some(([cmd]) => cmd === 'ai_abort_turn')).toBe(false);
    expect(useAiConversation.getState().messages[0]?.text).toBe(objective);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('The same investigation is complete.')).toBeTruthy();
    const prompts = tauriMock.invoke.mock.calls.filter(([cmd]) => cmd === 'ai_begin').map(([, args]) => String(args?.prompt ?? ''));
    expect(prompts).toHaveLength(2);
    expect(prompts[1]).toContain('Do not choose a different research question');
    expect(prompts[1]).toContain(objective);
    expect(useAiConversation.getState().messages[0]?.text).toBe(objective);
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
  it('describes blobs by size and passes text through in full', () => {
    expect(modelCell('ab'.repeat(5000), 'blob')).toBe('[blob, 5000 bytes]');
    expect(modelCell('x'.repeat(5000), 'v_wstring')).toBe('x'.repeat(5000));
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
    fireEvent.click(await screen.findByRole('button', { name: 'Choose model server or API' }));
    fireEvent.click(screen.getByRole('radio', { name: 'Model server / API' }));
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect(await screen.findByRole('option', { name: 'qwen2.5:7b' })).toBeTruthy();
    expect(tauriMock.invoke.mock.calls.find(([c]) => c === 'ai_set_provider')?.[1]).toMatchObject({ provider: 'local', kind: 'ollama', baseUrl: 'http://localhost:11434', contextTokens: 32768 });
    expect(screen.getByText('Ollama', { selector: '.ai-provider-badge' })).toBeTruthy();
    expect(screen.queryByRole('combobox', { name: 'Reasoning effort' })).toBeNull();
    expect(screen.getByText(/Data stays on this computer/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'hello' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('Local answer')).toBeTruthy();
    expect(tauriMock.invoke.mock.calls.find(([c]) => c === 'ai_respond')?.[1]).toMatchObject({ model: 'qwen2.5:7b', effort: null });
    expect(screen.getByText(/6\.4k this turn · 6\.4k total/)).toBeTruthy();
    expect(screen.getByRole('img', { name: /New input 6\.4k/ })).toBeTruthy();
  });

  it('connects to DeepSeek only with an API key and requires external data-routing consent', async () => {
    let active = false;
    tauriMock.invoke.mockImplementation(async (command: string) => {
      if (command === 'ai_status') return active
        ? { provider: 'local', local: { kind: 'openai', base_url: 'https://api.deepseek.com/v1', context_tokens: 32768, loopback: false }, connected: true, plan_usage_authorized: true, data_routing_allowed: false }
        : { provider: 'chatgpt', connected: false, plan_usage_authorized: false };
      if (command === 'ai_set_provider') { active = true; return [{ slug: 'deepseek-v4-pro', display_name: 'deepseek-v4-pro' }]; }
      if (command === 'ai_models') return active ? [{ slug: 'deepseek-v4-pro', display_name: 'deepseek-v4-pro', supported_efforts: [] }] : [];
      return undefined;
    });
    render(<AiSidebar close={() => undefined} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Choose model server or API' }));
    fireEvent.click(screen.getByRole('radio', { name: 'Model server / API' }));
    fireEvent.change(screen.getByLabelText('Server'), { target: { value: 'deepseek' } });
    expect((screen.getByLabelText('Address') as HTMLInputElement).value).toBe('https://api.deepseek.com');
    expect((screen.getByRole('button', { name: 'Connect' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/DeepSeek API key/), { target: { value: 'sk-private-test' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    expect(await screen.findByRole('option', { name: 'deepseek-v4-pro' })).toBeTruthy();
    expect(tauriMock.invoke.mock.calls.find(([c]) => c === 'ai_set_provider')?.[1]).toMatchObject({
      provider: 'local', kind: 'openai', baseUrl: 'https://api.deepseek.com', apiKey: 'sk-private-test',
    });
    expect((screen.getByLabelText(/DeepSeek API key/) as HTMLInputElement).value).toBe('');
    expect(screen.getByLabelText(/Allow prompts, workflow context and requested data results to be sent to the model server at https:\/\/api\.deepseek\.com\/v1/)).toBeTruthy();
    expect(screen.getByText(/billed by that provider and is separate from ChatGPT plan usage/)).toBeTruthy();
  });
});

describe('engine operations', () => {
  it('asks before a download in Review mode and sends the grant only after approval', async () => {
    const requests: Array<{ method: string; params: any }> = [];
    setTransport({ request: async (method: string, params: any) => {
      if (method !== 'agent.call') return { searxng_url: null };
      requests.push({ method, params });
      if (!params.permissions.length) return { ok: false, operation: 'download_file', error: { code: 'permission-required', message: 'x', permissions: ['network', 'file_writes'] } };
      return { ok: true, operation: 'download_file', result: { path: '/w/data/fuel.csv', sha256: 'f'.repeat(64) } };
    }, subscribe: () => () => {} } as any);
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_begin') return { turn_id: 't_dl', assistant_message_id: 'm_dl' };
      if (command === 'ai_respond') {
        if (!tauriMock.invoke.mock.calls.some(([c]) => c === 'ai_submit_tool_result')) return { turn_id: args?.turnId, message_id: 'm_dl', cycle_id: args?.cycleId, text: '',
          calls: [{ name: 'agent.download_file', call_id: 'dl', arguments: JSON.stringify({ url: 'https://example.org/fuel.csv' }) }] };
        return { turn_id: args?.turnId, message_id: 'm_dl', cycle_id: args?.cycleId, text: 'Downloaded.', calls: [] };
      }
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'Get the fuel data.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    const card = await screen.findByRole('region', { name: 'Permission request' });
    expect(card.textContent).toContain('Download https://example.org/fuel.csv');
    expect(requests).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }));
    expect(await screen.findByText('Downloaded.')).toBeTruthy();
    expect(requests[1].params).toMatchObject({ name: 'download_file', permissions: ['network', 'file_writes'] });
    const submitted = tauriMock.invoke.mock.calls.find(([c]) => c === 'ai_submit_tool_result')?.[1] as any;
    expect(submitted.result).toMatchObject({ ok: true, result: { path: '/w/data/fuel.csv' } });
  });
});

describe('completion requires execution evidence', () => {
  function scripted(final: { verification: string; classified_as_question?: string | null; evidence?: unknown[] }, gateTimes: number) {
    let responses = 0;
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_begin') return { turn_id: 't_gate', assistant_message_id: 'm_gate' };
      if (command === 'ai_respond') {
        responses++;
        if (responses <= gateTimes) return { turn_id: args?.turnId, message_id: 'm_gate', cycle_id: args?.cycleId, text: '', calls: [], needs_execution: true };
        return { turn_id: args?.turnId, message_id: 'm_gate', cycle_id: args?.cycleId, text: 'Category A totals 1,204.', calls: [] };
      }
      if (command === 'ai_finish_turn') return { verification: final.verification, classified_as_question: final.classified_as_question ?? null, evidence: final.evidence ?? [] };
      return undefined;
    });
    return () => responses;
  }

  it('does not present a text-only analysis as complete (Test F)', async () => {
    await ready();
    const count = scripted({ verification: 'unverified' }, 2);
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'Build a workflow that totals sales by category.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText(/Not verified: no Flowbench run backs this answer/)).toBeTruthy();
    expect(count()).toBe(3);
    expect(screen.getByText('Category A totals 1,204.')).toBeTruthy();
  });

  it('marks an answer backed by a run as verified', async () => {
    await ready();
    scripted({ verification: 'verified', evidence: [{ kind: 'run', run_id: 'run_42' }] }, 0);
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'Total sales by category.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect((await screen.findByText('✓ Backed by a Flowbench run')).getAttribute('title')).toBe('Run run_42');
  });
});

describe('task view', () => {
  it('shows phases, actions, runs, downloaded files, errors and the verdict', async () => {
    await ready();
    useAiConversation.getState().setTask({ id: 'turn_t', objective: 'Average fuel price by year', kind: 'execution', phase: 'inspect', status: 'completed',
      verification: 'verified', actions: [
        { at: 1, tool: 'agent.download_file', phase: 'acquire', ok: true, summary: 'ok', result_bytes: 3400000, model_bytes: 5800, result_ref: 'res_c1' },
        { at: 2, tool: 'workflow.request_run', phase: 'execute', ok: false, summary: 'failed: Unknown field [Pric]' },
        { at: 3, tool: 'workflow.propose_patch', phase: 'repair', ok: true, summary: 'approved' },
        { at: 4, tool: 'workflow.request_run', phase: 'execute', ok: true, summary: 'succeeded' }],
      runs: [{ run_id: 'r1', status: 'failed', outputs: 0 }, { run_id: 'r2abcdef99', status: 'succeeded', outputs: 2 }],
      artifacts: [{ kind: 'download', path: '/w/data/fuel.csv', url: 'https://gov.uk/fuel.csv', sha256: 'aa' }],
      revisions: ['rev_1'], errors: [{ tool: 'workflow.request_run', error: { message: 'Unknown field [Pric]' } }],
      evidence: [{ kind: 'run', run_id: 'r2abcdef99' }], started_at: 1, finished_at: 5 } as any);
    fireEvent.click(screen.getByRole('tab', { name: /Task/ }));
    const pane = await screen.findByRole('region', { name: 'Task details' });
    expect(pane.textContent).toContain('Average fuel price by year');
    expect(screen.getByRole('list', { name: 'Task phases' }).textContent).toContain('Repair');
    expect(screen.getByRole('list', { name: 'Actions' }).children).toHaveLength(4);
    expect(pane.textContent).toContain('succeeded · 2 outputs · r2abcdef');
    expect(pane.textContent).toContain('/w/data/fuel.csv');
    expect(pane.textContent).toContain('Unknown field [Pric]');
    expect(pane.textContent).toContain('Backed by Flowbench execution (1 piece of evidence).');
    expect(pane.textContent).toContain('3.2 MB kept · 5.7 kB sent');
  });
});

describe('manual and AI editing on one canvas (Test C)', () => {
  it('extends a hand-built workflow, keeps later manual edits, and undoes and redoes both in order', async () => {
    const manifest = (tool_id: string, inputs: string[], outputs: string[], config_schema: any = { type: 'object' }) => ({ tool_id, version: '1', display_name: tool_id,
      catalog_ids: [], inputs: inputs.map((name) => ({ name, label: name, required: true, multiple: false, description: '' })),
      outputs: outputs.map((name) => ({ name, label: name, required: false, multiple: false, description: '' })),
      config_schema, side_effects: 'none', deterministic: true, dependencies: [], description: '' });
    useEditor.getState().setEngineMeta({ tools: {
      'core.input_data': manifest('core.input_data', [], ['output']) as any,
      'core.auto_field': manifest('core.auto_field', ['input'], ['output']) as any,
      'core.filter': manifest('core.filter', ['input'], ['true', 'false']) as any,
      'core.summarize': manifest('core.summarize', ['input'], ['output']) as any,
      'core.sort': manifest('core.sort', ['input'], ['output']) as any,
    }, engineInfo: { engine: 'flowengine', version: 'test' } as any });
    setTransport({ request: async () => ({ valid: true, problems: [], schemas: {} }), subscribe: () => () => {} } as any);
    await ready();
    // 1. Three nodes built by hand.
    const editor = useEditor.getState();
    const read = editor.addTool('core.input_data', { x: 0, y: 0 });
    const types = useEditor.getState().addTool('core.auto_field', { x: 200, y: 0 });
    const keep = useEditor.getState().addTool('core.filter', { x: 400, y: 0 });
    useEditor.getState().connect({ source: read, sourceHandle: 'output', target: types, targetHandle: 'input' });
    useEditor.getState().connect({ source: types, sourceHandle: 'output', target: keep, targetHandle: 'input' });
    useEditor.getState().updateNodeData(keep, { config: { expression: '[Sales] > 0' } });
    const manual = structuredClone({ nodes: useEditor.getState().nodes, edges: useEditor.getState().edges });
    // 2. The AI completes it from the filter's True output (node_3).
    const patch = JSON.stringify({ operations: [
      { op: 'add', ref: 'totals', tool_id: 'core.summarize', config: { actions: [{ field: 'Region', op: 'group_by' }, { field: 'Sales', op: 'sum' }] }, position: { x: 600, y: 0 } },
      { op: 'add', ref: 'ranked', tool_id: 'core.sort', config: { keys: [{ field: 'Sum_Sales', order: 'desc' }] }, position: { x: 800, y: 0 } },
      { op: 'connect', source_ref: 'node_3', source_anchor: 'true', target_ref: 'totals', target_anchor: 'input' },
      { op: 'connect', source_ref: 'totals', source_anchor: 'output', target_ref: 'ranked', target_anchor: 'input' },
    ] });
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_begin') return { turn_id: 't_c', assistant_message_id: 'm_c' };
      if (command === 'ai_respond') {
        if (!tauriMock.invoke.mock.calls.some(([c]) => c === 'ai_submit_tool_result')) return { turn_id: args?.turnId, message_id: 'm_c', cycle_id: args?.cycleId, text: '',
          calls: [{ name: 'workflow.propose_patch', call_id: 'p', arguments: JSON.stringify({ patch_json: patch, base_revision: workflowRevision(useEditor.getState()) }) }] };
        return { turn_id: args?.turnId, message_id: 'm_c', cycle_id: args?.cycleId, text: 'Added totals and ranking.', calls: [] };
      }
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'Finish this workflow: total sales by region, highest first.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Approve changes' }));
    expect(await screen.findByText('Added totals and ranking.')).toBeTruthy();
    const afterAi = useEditor.getState();
    expect(afterAi.nodes).toHaveLength(5);
    expect(afterAi.nodes.slice(0, 3).map((n) => n.id)).toEqual([read, types, keep]);
    expect(afterAi.edges.some((e) => e.source === keep && e.sourceHandle === 'true')).toBe(true);
    // 3. A manual edit to a node the AI added.
    const sortNode = afterAi.nodes.find((n) => (n.data as any).tool === 'core.sort')!;
    useEditor.getState().updateNodeData(sortNode.id, { config: { keys: [{ field: 'Sum_Sales', order: 'asc' }] } });
    expect((useEditor.getState().nodes.find((n) => n.id === sortNode.id)!.data as any).config.keys[0].order).toBe('asc');
    // 4. Undo steps back through the manual edit, then the whole AI change as one step; redo replays them.
    useEditor.getState().undo();
    expect((useEditor.getState().nodes.find((n) => n.id === sortNode.id)!.data as any).config.keys[0].order).toBe('desc');
    useEditor.getState().undo();
    expect(useEditor.getState().nodes.map((n) => n.id)).toEqual(manual.nodes.map((n) => n.id));
    expect(useEditor.getState().edges).toHaveLength(manual.edges.length);
    useEditor.getState().redo();
    expect(useEditor.getState().nodes).toHaveLength(5);
    useEditor.getState().redo();
    expect((useEditor.getState().nodes.find((n) => n.id === sortNode.id)!.data as any).config.keys[0].order).toBe('asc');
  });
});

describe('permissions last for the task', () => {
  it('asks once before running programs and reuses the grant for later steps of the same task', async () => {
    const requests: any[] = [];
    setTransport({ request: async (method: string, params: any) => {
      if (method !== 'agent.call') return { searxng_url: null };
      requests.push(params);
      if (!params.permissions.includes('processes')) return { ok: false, operation: 'run_python', error: { code: 'permission-required', message: 'x', permissions: ['processes'] } };
      return { ok: true, operation: 'run_python', result: { exit_code: 0, stdout: '', stderr: '', files_written: [] } };
    }, subscribe: () => () => {} } as any);
    await ready();
    let step = 0;
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_begin') return { turn_id: 't_p', assistant_message_id: 'm_p' };
      if (command === 'ai_respond') {
        step++;
        if (step <= 2) return { turn_id: args?.turnId, message_id: 'm_p', cycle_id: args?.cycleId, text: '',
          calls: [{ name: 'agent.run_python', call_id: `py${step}`, arguments: JSON.stringify({ code: `print(${step})` }) }] };
        return { turn_id: args?.turnId, message_id: 'm_p', cycle_id: args?.cycleId, text: 'Both scripts ran.', calls: [] };
      }
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'Prepare the data with Python.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    const card = await screen.findByRole('region', { name: 'Permission request' });
    expect(card.textContent).toContain('Allow running programs for this task?');
    expect(card.textContent).toContain('print(1)');
    fireEvent.click(screen.getByRole('button', { name: 'Allow' }));
    expect(await screen.findByText('Both scripts ran.')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Permission request' })).toBeNull();
    expect(requests.map((r) => r.permissions.includes('processes'))).toEqual([false, true, true]);
  });
});

describe('deliverables and web search settings', () => {
  it('lists deliverables and what is missing, and labels an incomplete answer', async () => {
    await ready();
    tauriMock.invoke.mockImplementation(async (command: string, args?: Record<string, unknown>) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_begin') return { turn_id: 't_dv', assistant_message_id: 'm_dv' };
      if (command === 'ai_respond') return { turn_id: args?.turnId, message_id: 'm_dv', cycle_id: args?.cycleId, text: 'Here is one chart.', calls: [] };
      if (command === 'ai_finish_turn') return { verification: 'incomplete', evidence: [{ run_id: 'r1' }], missing: ['2 verified chart(s) requested, 1 delivered'] };
      return undefined;
    });
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'Make two charts of fuel prices.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText(/Incomplete: missing 2 verified chart\(s\) requested, 1 delivered/)).toBeTruthy();
    expect(tauriMock.invoke.mock.calls.find(([c]) => c === 'ai_begin')?.[1]).toHaveProperty('workspace');
    useAiConversation.getState().setTask({ id: 't_dv', objective: 'Make two charts', kind: 'execution', phase: 'inspect', status: 'incomplete',
      verification: 'incomplete', actions: [], runs: [], artifacts: [], revisions: [], errors: [], evidence: [], started_at: 1,
      deliverables: [{ id: 'c1', kind: 'chart', description: 'Price chart', artifact: { path: 'out/prices.html' }, status: 'verified' },
                     { id: 'c2', kind: 'chart', description: 'Premium chart', artifact: { path: 'out/x.txt' }, status: 'failed', problem: 'A chart must be an image, SVG, HTML or PDF file, not .txt' }],
      missing: ['c2 (chart): A chart must be an image, SVG, HTML or PDF file, not .txt'] } as any);
    fireEvent.click(screen.getByRole('tab', { name: /Task/ }));
    const list = await screen.findByRole('list', { name: 'Deliverables' });
    expect(list.children).toHaveLength(2);
    expect(list.textContent).toContain('out/prices.html');
    expect(list.closest('section')!.textContent).toContain('Finished with deliverables missing');
    expect(list.closest('section')!.textContent).toContain('some requested deliverables are missing');
  });

  it('saves and tests a SearXNG address through the engine', async () => {
    const calls: any[] = [];
    setTransport({ request: async (method: string, params: any) => {
      calls.push({ method, params });
      return params.searxng_url ? { searxng_url: params.searxng_url, test: { results: 10, unresponsive_engines: [['google', 'access denied']] } } : { searxng_url: null };
    }, subscribe: () => () => {} } as any);
    await ready();
    fireEvent.click(screen.getByRole('button', { name: 'AI settings' }));
    fireEvent.change(screen.getByLabelText('SearXNG address'), { target: { value: 'http://localhost:8888' } });
    fireEvent.change(screen.getByLabelText('Engines (optional)'), { target: { value: 'yandex,bing' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save and test' }));
    expect(await screen.findByText(/SearXNG answered with 10 result\(s\); engines not responding: google/)).toBeTruthy();
    expect(calls[calls.length - 1]).toEqual({ method: 'agent.search_settings', params: { searxng_url: 'http://localhost:8888', searxng_engines: 'yandex,bing', test: true } });
  });
});

describe('saving from the AI', () => {
  it('resolves save paths inside the workspace and refuses paths outside it', () => {
    expect(agentSavePath('fuel/prices.flow.json', '/home/u/proj', null, 'x')).toEqual({ path: '/home/u/proj/fuel/prices.flow.json' });
    expect(agentSavePath('/home/u/proj/a.flow.json', '/home/u/proj', null, null)).toEqual({ path: '/home/u/proj/a.flow.json' });
    expect(agentSavePath('../other/a.flow.json', '/home/u/proj', null, null)).toEqual({ outside: '../other/a.flow.json' });
    expect(agentSavePath('/etc/a.flow.json', '/home/u/proj', null, null)).toEqual({ outside: '/etc/a.flow.json' });
    expect(agentSavePath('out\\a.flow.json', 'C:\\Users\\u\\proj', null, null)).toEqual({ path: 'C:\\Users\\u\\proj\\out\\a.flow.json' });
    expect(agentSavePath('', '/home/u/proj', '/home/u/proj/now.flow.json', 'x')).toEqual({ path: '/home/u/proj/now.flow.json' });
    expect(agentSavePath('', '/home/u/proj', null, 'UK fuel prices')).toEqual({ path: '/home/u/proj/UK_fuel_prices.flow.json' });
    expect(agentSavePath('', '/home/u/proj', null, null)).toBeNull();
  });
});
