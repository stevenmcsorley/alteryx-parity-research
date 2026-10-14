import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useEditor } from '../state/store';
import { AiSidebar } from './AiSidebar';

beforeEach(() => {
  useEditor.getState().newWorkflow();
  useEditor.setState({ name: 'Fixture workflow', catalog: [{ id: 'preparation:filter', name: 'Filter', category: 'Preparation',
    implementation_status: 'implemented', verification_status: 'partial', kernel: 'core.filter', documentation_url: null,
    external_prerequisites_blocking: false, unavailable_reason: null }] });
});
afterEach(cleanup);

describe('Flowbench AI workspace shell', () => {
  it('makes provider and data-routing status explicit', () => {
    render(<AiSidebar close={() => undefined} />);
    expect(screen.getByText(/No model connected/)).toBeTruthy();
    expect(screen.getByText(/does not send workflow data/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Continue with ChatGPT' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('searches the installed catalogue through the local read-only tool API', async () => {
    render(<AiSidebar close={() => undefined} />);
    fireEvent.change(screen.getByLabelText('Search Flowbench tool catalogue'), { target: { value: 'filter' } });
    fireEvent.submit(screen.getByLabelText('Search Flowbench tool catalogue').closest('form')!);
    const output = await screen.findByLabelText('Tool result');
    expect(output.textContent).toContain('preparation:filter');
  });
});
