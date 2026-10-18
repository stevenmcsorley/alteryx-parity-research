import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ServiceConnections } from './ServiceConnections';
import { SchemaForm } from './SchemaForm';
import { setTransport } from '../state/engineActions';

const saved = [{ id: 'svc_1', name: 'Local API', kind: 'web', base_url: 'https://api.example.com', auth: 'bearer', username: null, header_name: null }];

function stub() {
  const calls: Array<{ method: string; params: any }> = [];
  setTransport({ request: async (method: string, params: any) => {
    calls.push({ method, params });
    if (method === 'credentials.list') return saved;
    if (method === 'credentials.save') return { connection: { ...params.connection, id: 'svc_2' }, credential_saved: !!params.secret };
    return {};
  }, subscribe: () => () => {} } as any);
  return calls;
}

describe('Web & API connections', () => {
  afterEach(cleanup);

  it('saves a GitHub connection with its token passed separately from the settings', async () => {
    const calls = stub();
    render(<ServiceConnections close={vi.fn()} />);
    await screen.findByText('Local API');
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'GitHub' } });
    fireEvent.change(screen.getByLabelText('Connection type'), { target: { value: 'github' } });
    expect((screen.getByLabelText('Base URL') as HTMLInputElement).value).toBe('https://api.github.com');
    fireEvent.change(screen.getByLabelText('Secret'), { target: { value: 'ghp_secret' } });
    fireEvent.click(screen.getByText('Save'));
    await screen.findByText('Connection saved.');
    const save = calls.find((c) => c.method === 'credentials.save')!;
    expect(save.params.secret).toBe('ghp_secret');
    expect(save.params.connection).toMatchObject({ name: 'GitHub', kind: 'github', auth: 'bearer', base_url: 'https://api.github.com' });
    expect(JSON.stringify(save.params.connection)).not.toContain('ghp_secret');
  });

  it('asks only for GitHub connections when a tool requires one', async () => {
    const calls = stub();
    render(<SchemaForm schema={{ type: 'object', required: ['connection_id'], properties: { connection_id: { type: 'string', 'x-flowbench-connection': 'github' } } } as any}
      value={{ connection_id: '' }} fields={{ all: [], byAnchor: {} } as any} idPrefix="gh" onBeginEdit={vi.fn()} onChange={vi.fn()} />);
    await screen.findByText('Choose a GitHub connection…');
    expect(calls.find((c) => c.method === 'credentials.list')?.params).toEqual({ kind: 'github' });
  });

  it('offers saved connections for a connection_id setting', async () => {
    stub();
    const onChange = vi.fn();
    render(<SchemaForm schema={{ type: 'object', properties: { connection_id: { type: ['string', 'null'] } } } as any}
      value={{ connection_id: null }} fields={{ all: [], byAnchor: {} } as any} idPrefix="dl" onBeginEdit={vi.fn()} onChange={onChange} />);
    await waitFor(() => expect(screen.getByText('Local API — https://api.example.com')).toBeTruthy());
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'svc_1' } });
    expect(onChange).toHaveBeenCalledWith({ connection_id: 'svc_1' });
  });

  it('signs in an OAuth connection with a device code and polls until it is signed in', async () => {
    const polls: string[] = ['pending', 'signed_in'];
    const oauth = { id: 'svc_o', name: 'Graph', kind: 'web', base_url: 'https://graph.microsoft.com/v1.0', auth: 'oauth2', username: null,
      header_name: null, token_url: 'https://login.microsoftonline.com/t/oauth2/v2.0/token', client_id: 'app-1', scope: 'Files.Read offline_access',
      device_code_url: 'https://login.microsoftonline.com/t/oauth2/v2.0/devicecode' };
    const calls: string[] = [];
    setTransport({ request: async (method: string) => {
      calls.push(method);
      if (method === 'credentials.list') return [oauth];
      if (method === 'credentials.oauth.device_start') return { device_code: 'dc', user_code: 'ABCD-EFGH', verification_uri: 'https://microsoft.com/devicelogin', interval: 0, expires_in: 60 };
      if (method === 'credentials.oauth.device_poll') return { status: polls.shift() };
      return {};
    }, subscribe: () => () => {} } as any);
    render(<ServiceConnections close={vi.fn()} />);
    fireEvent.click(await screen.findByText('Graph'));
    fireEvent.click(screen.getByText('Sign in…'));
    expect(await screen.findByText('ABCD-EFGH')).toBeTruthy();
    await screen.findByText(/Signed in\. The refresh token is saved/);
    expect(calls.filter((c) => c === 'credentials.oauth.device_poll')).toHaveLength(2);
  });
});
