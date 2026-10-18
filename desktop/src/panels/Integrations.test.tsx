import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Integrations } from './Integrations';
import { setTransport } from '../state/engineActions';

const spark = { id: 'spark-local', family: 'spark', name: 'Apache Spark (local mode)', kind: 'local-runtime', serves: ['developer:apache-spark-code'],
  licence: 'Apache-2.0', setup: 'pip install pyspark', verification: 'spark-runtime CI job', semantics: { timezone: 'UTC' }, ready: false,
  checks: [{ need: 'python:pyspark', ok: true, detail: 'Python package pyspark' }, { need: 'exe:java', ok: false, detail: 'java not found on PATH' }] };

function stub() {
  const calls: Array<{ method: string; params: any }> = [];
  let entitlements: any[] = [];
  setTransport({ request: async (method: string, params: any) => {
    calls.push({ method, params });
    if (method === 'integrations.providers') return [spark];
    if (method === 'integrations.entitlements.list') return entitlements;
    if (method === 'integrations.entitlements.declare') { entitlements = [{ id: 'ent_1', product: params.product, note: params.note, declared_at: 1, validated: false }]; return entitlements[0]; }
    if (method === 'integrations.availability') return [{ catalog_id: 'developer:apache-spark-code', available: false,
      providers: [{ provider: 'spark-local', name: 'Apache Spark (local mode)', kind: 'local-runtime', ready: false, missing: ['java not found on PATH'] }] }];
    return [];
  }, subscribe: () => () => {} } as any);
  return calls;
}

describe('Integrations & Licences', () => {
  afterEach(cleanup);

  it('records a licence as declared, never validated', async () => {
    const calls = stub();
    render(<Integrations close={vi.fn()} initial="Licences" />);
    await screen.findByText('No licences recorded.');
    fireEvent.change(screen.getByLabelText('Licensed product'), { target: { value: 'Alteryx Server' } });
    fireEvent.click(screen.getByText('Record licence'));
    await screen.findByText('declared, not validated');
    expect(calls.find((c) => c.method === 'integrations.entitlements.declare')?.params).toEqual({ product: 'Alteryx Server', note: '' });
  });

  it('shows what each provider and tool still needs', async () => {
    stub();
    render(<Integrations close={vi.fn()} initial="Providers" />);
    await screen.findByText('Apache Spark (local mode)');
    expect(screen.getByText('needs setup')).toBeTruthy();
    expect(screen.getByText(/java not found on PATH/)).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: 'Tool availability' }));
    await waitFor(() => expect(screen.getByText('developer:apache-spark-code')).toBeTruthy());
    expect(screen.getByText(/: java not found on PATH/)).toBeTruthy();
  });
});
