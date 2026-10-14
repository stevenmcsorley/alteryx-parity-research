import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEditor } from '../state/store';
import { AiSidebar } from './AiSidebar';

const tauriMock = vi.hoisted(() => ({ invoke: vi.fn(), handler: null as null | ((event: { payload: any }) => void) }));
vi.mock('@tauri-apps/api/core', () => ({ invoke: tauriMock.invoke }));
vi.mock('@tauri-apps/api/event', () => ({ listen: vi.fn(async (_name: string, handler: (event: { payload: any }) => void) => {
  tauriMock.handler = handler; return () => { tauriMock.handler = null; };
}) }));

beforeEach(() => {
  tauriMock.handler = null;
  tauriMock.invoke.mockReset().mockImplementation(async (command: string) => {
    if (command === 'ai_status') return { connected: false, plan_usage_authorized: false };
    if (command === 'ai_sign_in') return undefined;
    return [];
  });
  useEditor.getState().newWorkflow();
  useEditor.setState({ name: 'Fixture workflow', catalog: [{ id: 'preparation:filter', name: 'Filter', category: 'Preparation',
    implementation_status: 'implemented', verification_status: 'partial', kernel: 'core.filter', documentation_url: null,
    external_prerequisites_blocking: false, unavailable_reason: null }] });
});
afterEach(cleanup);

describe('Flowbench AI workspace shell', () => {
  it('makes provider and data-routing status explicit', async () => {
    render(<AiSidebar close={() => undefined} />);
    expect(await screen.findByText('Connect ChatGPT')).toBeTruthy();
    expect(screen.getByText(/External model receives your prompt/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Continue with ChatGPT' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('searches the installed catalogue through the local read-only tool API', async () => {
    render(<AiSidebar close={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Search Flowbench tool catalogue'), { target: { value: 'filter' } });
    fireEvent.submit(screen.getByLabelText('Search Flowbench tool catalogue').closest('form')!);
    const output = await screen.findByLabelText('Tool result');
    expect(output.textContent).toContain('preparation:filter');
  });

  it('streams a model response and routes a registered read-only function to Flowbench', async () => {
    let responseNumber = 0;
    tauriMock.invoke.mockImplementation(async (command: string) => {
      if (command === 'ai_status') return { connected: true, plan_usage_authorized: true, subject_hint: 'Account · safe' };
      if (command === 'ai_models') return [{ slug: 'model-a', display_name: 'Available model' }];
      if (command === 'ai_respond') {
        const first = responseNumber++ === 0;
        tauriMock.handler?.({ payload: { type: 'delta', text: first ? 'Checking the workflow. ' : 'It currently has no nodes.' } });
        return { calls: first ? [{ name: 'workflow.get_snapshot', call_id: 'call-1', arguments: '{}' }] : [] };
      }
      return undefined;
    });
    render(<AiSidebar close={() => undefined} />);
    await screen.findByRole('option', { name: 'Available model' });
    fireEvent.click(screen.getByRole('checkbox', { name: /I approve sending my prompt/ }));
    await waitFor(() => expect((screen.getByRole('checkbox', { name: /I approve sending my prompt/ }) as HTMLInputElement).checked).toBe(true));
    fireEvent.change(screen.getByLabelText('Message Flowbench AI'), { target: { value: 'What does this workflow contain?' } });
    await waitFor(() => expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('Using workflow.get_snapshot')).toBeTruthy();
    expect(await screen.findByText('It currently has no nodes.')).toBeTruthy();
    expect(tauriMock.invoke.mock.calls.filter(([command]) => command === 'ai_respond')).toHaveLength(2);
    expect(tauriMock.invoke.mock.calls.some(([command, args]) => command === 'ai_respond' && JSON.stringify(args) === JSON.stringify({ model: 'model-a' }))).toBe(true);
    expect(tauriMock.invoke.mock.calls.some(([command, args]) => command === 'ai_submit_tool_result' && args.callId === 'call-1')).toBe(true);
    expect(tauriMock.invoke.mock.calls.some(([command, args]) => command === 'ai_begin' && args.prompt === 'What does this workflow contain?')).toBe(true);
  });
});
