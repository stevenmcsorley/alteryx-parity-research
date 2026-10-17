import { describe, expect, it } from 'vitest';
import { activityLabel, addUsage, diagnosticSummary, NO_USAGE, tokens } from './panelText';

const state = { tools: { 'core.summarize': { display_name: 'Summarize' } }, nodes: [
  { id: 'a', data: { kind: 'tool', tool: 'core.input_data' } }, { id: 'b', data: { kind: 'tool', tool: 'core.summarize' } }] } as any;

describe('AI panel text', () => {
  it('describes each step with its arguments and the canvas name of the node', () => {
    expect(activityLabel('run.get_result_page', { node_ref: 'node_2', offset: 0, limit: 40, columns: 'Region,Total' }, state))
      .toBe('Reading rows 1–40 of Summarize (node 2), 2 columns…');
    expect(activityLabel('catalog.search_tools', { query: 'fuzzy' })).toBe('Searching tools for “fuzzy”…');
    expect(activityLabel('workflow.propose_patch', { patch_json: JSON.stringify({ operations: [{}, {}, {}] }) })).toBe('Preparing 3 workflow changes…');
    expect(activityLabel('workflow.get_node_config', { node_ref: 'node_9' }, state)).toBe('Reading the settings of node_9…');
  });

  it('summarises responses in plain language', () => {
    expect(diagnosticSummary({ terminal_status: 'stream_error', error_code: 'usage_limit_reached' })).toMatchObject({ problem: true, text: expect.stringMatching(/usage limit/) });
    expect(diagnosticSummary({ terminal_status: 'stream_error', error_code: 'provider_stream_error' }).text).toMatch(/usage limit may have been reached/);
    expect(diagnosticSummary({ terminal_status: 'completed', usage: { input_tokens: 12_400, output_tokens: 300 } })).toEqual({ problem: false, text: 'Completed · 12k in · 300 out' });
  });

  it('adds up token usage', () => {
    expect(addUsage(addUsage(NO_USAGE, { input_tokens: 1000, output_tokens: 5 }), { input_tokens: null })).toEqual({ input: 1000, output: 5 });
    expect([tokens(999), tokens(1500), tokens(469_166), tokens(3_200_000)]).toEqual(['999', '1.5k', '469k', '3.2M']);
  });
});
