import { expect, test, type Page } from '@playwright/test';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

const REPO = path.resolve(process.cwd(), '..');
const EXAMPLE = path.join(REPO, 'examples', 'sales', 'monthly_sales.flow.json');

async function ready(page: Page) {
  await page.goto('/');
  await expect(page.getByText(/engine \d/)).toBeVisible();
  await expect(page.getByTestId('palette-list')).toBeVisible();
}

async function addFromPalette(page: Page, catalogId: string) {
  await page.getByTestId(`palette-${catalogId}`).dblclick();
}

async function connect(page: Page, from: string, to: string) {
  const src = page.getByTestId(from);
  const dst = page.getByTestId(to);
  await src.hover();
  await page.mouse.down();
  const box = (await dst.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 12 });
  await page.mouse.up();
}

async function openExample(page: Page, file = EXAMPLE) {
  await page.getByRole('toolbar').getByRole('button', { name: 'Open', exact: true }).click();
  await page.locator('#path-input').fill(file);
  await page.getByTestId('dialog-ok').click();
}

test('palette lists every catalogue entry with an honest status', async ({ page }) => {
  await ready(page);
  const items = page.locator('[data-testid^="palette-"].tool-item');
  await expect(items).toHaveCount(323);
  const implemented = await page.locator('.tool-item .badge.ready, .tool-item .badge.tested, .tool-item .badge.refdiff').count();
  expect(implemented).toBeGreaterThanOrEqual(26);
  await page.getByTestId('palette-address:us-geocoder').click();
  await expect(page.getByTestId('tool-detail')).toContainText('licensed data');
  await expect(page.getByTestId('palette-address:us-geocoder')).toHaveAttribute('draggable', 'false');
  await page.getByLabel('Search tools').fill('join');
  await expect(page.getByTestId('palette-join:join')).toBeVisible();
  await expect(page.getByTestId('palette-preparation:filter')).toHaveCount(0);
  await page.getByLabel('Search tools').fill('');
  await page.getByText(/Implemented only/).click();
  await expect(items).toHaveCount(implemented);
});

test('build, configure, connect and run a workflow from scratch', async ({ page }) => {
  await ready(page);
  await addFromPalette(page, 'in-out:text-input');
  const editor = page.getByTestId('text-input-editor');
  await editor.getByRole('button', { name: '+ field' }).click();
  await editor.getByRole('button', { name: '+ field' }).click();
  await editor.getByLabel('Field 1 name').fill('city');
  await editor.getByLabel('Field 2 name').fill('sales');
  await editor.getByLabel('Field 2 type').selectOption('int32');
  for (const [c, n] of [['Leeds', '40'], ['York', '7'], ['Hull', '']]) {
    await editor.getByRole('button', { name: '+ row' }).click();
    const r = await editor.locator('tbody tr').count();
    await editor.getByLabel(`Row ${r} field 1`).fill(c);
    if (n) await editor.getByLabel(`Row ${r} field 2`).fill(n);
  }
  await addFromPalette(page, 'preparation:filter');
  await page.getByRole('radio', { name: 'Custom expression' }).click();
  await page.getByTestId('cfg-filter_1-expression-expr').fill('[sales] > 10');
  await connect(page, 'out-text_input_1-output', 'in-filter_1-input');
  await expect(page.getByTestId('valid')).toBeVisible({ timeout: 10_000 });
  await page.getByTestId('run').click();
  await expect(page.getByTestId('run-status')).toContainText('succeeded', { timeout: 30_000 });
  await expect(page.getByTestId('rows-filter_1-true')).toHaveText('1');
  await expect(page.getByTestId('rows-filter_1-false')).toHaveText('2');
  await page.getByLabel('Output to display').selectOption('filter_1::true');
  await expect(page.getByTestId('results-grid')).toContainText('Leeds');
  await expect(page.getByTestId('results-grid')).not.toContainText('York');
  await page.getByTestId('tab-fields').click();
  await expect(page.getByTestId('fields-table')).toContainText('int32');
});

test('CSV text typing is visible in Filter and changes only after an explicit numeric declaration', async ({ page }) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowbench-type-audit-'));
  const csv = path.join(dir, 'quantities.csv');
  const quantities = ['0', '1', '10', '09', '00123', '-2', '2', '3', '5', '6', '11', '12', '20', '50', '100'];
  fs.writeFileSync(csv, `Quantity,Sales\n${quantities.map((q) => `${q},1.25`).join('\n')}\n`);
  await ready(page);
  await addFromPalette(page, 'in-out:input-data');
  await page.getByLabel('Path').fill(csv);
  await addFromPalette(page, 'preparation:filter');
  await connect(page, 'out-input_data_1-output', 'in-filter_1-input');
  await expect(page.getByTestId('valid')).toBeVisible({ timeout: 10_000 });
  await page.getByRole('radio', { name: /Basic:/ }).click();
  await page.getByLabel('Field', { exact: true }).fill('Quantity');
  await page.getByLabel('Operator').selectOption('>');
  await page.getByLabel('Value').fill('10');
  await addFromPalette(page, 'in-out:browse');
  await connect(page, 'out-filter_1-true', 'in-browse_1-input');
  await page.getByTestId('run').click();
  await expect(page.getByTestId('run-status')).toContainText('succeeded', { timeout: 30_000 });
  await page.getByTestId('node-filter_1').click();
  await expect(page.getByTestId('inspector')).toContainText('Quantity is v_wstring; this comparison is lexicographic');
  await expect(page.getByTestId('rows-filter_1-true')).toHaveText('9');
  await expect(page.getByTestId('rows-filter_1-false')).toHaveText('6');
  await page.getByLabel('Output to display').selectOption('browse_1::(browse)');
  await expect(page.getByTestId('results-grid')).toContainText('2');

  await page.getByTestId('node-input_data_1').click();
  await page.getByRole('button', { name: '+ Add field' }).click();
  await page.locator('#cfg-input_data_1-fields-0-name').fill('Quantity');
  await page.locator('#cfg-input_data_1-fields-0-type').selectOption('int32');
  await page.getByRole('button', { name: '+ Add field' }).click();
  await page.locator('#cfg-input_data_1-fields-1-name').fill('Sales');
  await page.locator('#cfg-input_data_1-fields-1-type').selectOption('fixed_decimal');
  await page.locator('#cfg-input_data_1-fields-1-size').fill('8');
  await page.locator('#cfg-input_data_1-fields-1-scale').fill('2');
  await page.getByTestId('node-filter_1').click();
  await expect(page.getByTestId('inspector')).not.toContainText('Quantity is v_wstring');
  await page.getByTestId('run').click();
  await expect(page.getByTestId('run-status')).toContainText('succeeded', { timeout: 30_000 });
  await expect(page.getByTestId('rows-filter_1-true')).toHaveText('6');
  await expect(page.getByTestId('rows-filter_1-false')).toHaveText('9');
  await page.getByLabel('Output to display').selectOption('browse_1::(browse)');
  await expect(page.getByTestId('results-grid').locator('tbody tr')).toHaveCount(6);
  await expect(page.getByTestId('results-grid').locator('tbody tr td:nth-child(2)'))
    .toHaveText(['123', '11', '12', '20', '50', '100']);
});

test('configure Fuzzy Match through field selectors and execute indexed matching', async ({ page }) => {
  await ready(page);
  await addFromPalette(page, 'in-out:text-input');
  const editor = page.getByTestId('text-input-editor');
  await editor.getByRole('button', { name: '+ field' }).click();
  await editor.getByRole('button', { name: '+ field' }).click();
  await editor.getByRole('button', { name: '+ field' }).click();
  await editor.getByLabel('Field 1 name').fill('id');
  await editor.getByLabel('Field 1 type').selectOption('int32');
  await editor.getByLabel('Field 2 name').fill('name');
  await editor.getByLabel('Field 3 name').fill('source');
  for (const [name, source] of [['Robert', 'A'], ['Rupert', 'A'], ['Jones', 'B']]) {
    await editor.getByRole('button', { name: '+ row' }).click();
    const row = await editor.locator('tbody tr').count();
    await editor.getByLabel(`Row ${row} field 1`).fill(String(row));
    await editor.getByLabel(`Row ${row} field 2`).fill(name);
    await editor.getByLabel(`Row ${row} field 3`).fill(source);
  }
  await addFromPalette(page, 'join:fuzzy-match');
  await connect(page, 'out-text_input_1-output', 'in-fuzzy_match_1-input');
  await page.getByLabel('Record id field').selectOption('id');
  await page.getByRole('button', { name: '+ Add match field' }).click();
  await page.locator('#cfg-fuzzy_match_1-match_fields-0-field').selectOption('name');
  await page.locator('#cfg-fuzzy_match_1-match_fields-0-key_method').selectOption('soundex');
  await page.locator('#cfg-fuzzy_match_1-match_fields-0-match_method').selectOption('levenshtein');
  await page.locator('#cfg-fuzzy_match_1-match_fields-0-threshold').fill('0');
  await page.locator('#cfg-fuzzy_match_1-match_threshold').fill('40');
  await expect(page.getByTestId('valid')).toBeVisible({ timeout: 10_000 });
  await page.getByTestId('run').click();
  await expect(page.getByTestId('run-status')).toContainText('succeeded', { timeout: 30_000 });
  await page.getByLabel('Output to display').selectOption('fuzzy_match_1::output');
  await expect(page.getByTestId('results-grid')).toContainText('1');
  await expect(page.getByTestId('results-grid')).toContainText('2');
  await page.getByLabel('Mode').selectOption('merge');
  await page.getByLabel('Source id field').selectOption('source');
  await expect(page.getByTestId('valid')).toBeVisible({ timeout: 10_000 });
  await page.getByTestId('run').click();
  await expect(page.getByTestId('run-status')).toContainText('succeeded', { timeout: 30_000 });
  await page.getByLabel('Output to display').selectOption('fuzzy_match_1::output');
  await expect(page.getByTestId('result-count')).toContainText('0 rows');
});

test('run a typed analytic application and apply its configuration action', async ({ page }) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowbench-app-'));
  const wf = path.join(dir, 'application.flow.json');
  fs.writeFileSync(wf, JSON.stringify({
    format: 'flowengine.workflow', formatVersion: 1, name: 'question-driven',
    metadata: { application: {
      questions: [{ id: 'calculation', label: 'Calculation', type: 'choice', options: ['[amount] + 1', '[amount] + 4'], default: '[amount] + 1' }],
      actions: [{ question: 'calculation', node_id: 'calc', config_path: ['formulas', '0', 'expression'] }],
    } },
    nodes: [
      { id: 'source', tool: 'core.text_input', config: { fields: [{ name: 'amount', type: 'int32' }], rows: [[3]] } },
      { id: 'calc', tool: 'core.formula', config: { formulas: [{ field: 'result', expression: '[amount] + 1' }] } },
      { id: 'browse', tool: 'core.browse', config: {} },
    ],
    edges: [
      { from: { node: 'source', anchor: 'output' }, to: { node: 'calc', anchor: 'input' } },
      { from: { node: 'calc', anchor: 'output' }, to: { node: 'browse', anchor: 'input' } },
    ],
  }));
  await ready(page);
  await openExample(page, wf);
  await expect(page.getByTestId('valid')).toBeVisible({ timeout: 10_000 });
  await page.getByTestId('run').click();
  await page.getByRole('dialog', { name: 'Run analytic application' }).getByLabel('Calculation').selectOption('[amount] + 4');
  await page.getByRole('dialog').getByRole('button', { name: 'Run' }).click();
  await expect(page.getByTestId('run-status')).toContainText('succeeded', { timeout: 30_000 });
  await page.getByLabel('Output to display').selectOption('browse::(browse)');
  await expect(page.getByTestId('results-grid')).toContainText('7');
  // The question action is run-scoped; opening/saving still preserves the original node config.
  const saved = JSON.parse(fs.readFileSync(wf, 'utf-8'));
  expect(saved.nodes[1].config.formulas[0].expression).toBe('[amount] + 1');
});

test('palette Action configures a target through a non-data connection and runs', async ({ page }) => {
  await ready(page);
  await addFromPalette(page, 'in-out:text-input');
  const editor = page.getByTestId('text-input-editor');
  await editor.getByRole('button', { name: '+ field' }).click();
  await editor.getByLabel('Field 1 name').fill('amount');
  await editor.getByLabel('Field 1 type').selectOption('int32');
  for (const n of ['2', '5', '8']) {
    await editor.getByRole('button', { name: '+ row' }).click();
    const row = await editor.locator('tbody tr').count();
    await editor.getByLabel(`Row ${row} field 1`).fill(n);
  }
  await addFromPalette(page, 'preparation:filter');
  await page.getByRole('radio', { name: 'Custom expression' }).click();
  await page.getByTestId('cfg-filter_1-expression-expr').fill('[amount] > 2');
  await addFromPalette(page, 'interface:action');
  await page.getByLabel('Action question ID').fill('minimum_expression');
  await page.getByLabel('Action prompt').fill('Filter expression');
  await page.getByLabel('Action default answer').fill('[amount] > 2');
  // The minimap is a fixed overlay; don't let it intercept the connector drag in this UI acceptance case.
  await page.getByTestId('rf__minimap').evaluate((el) => { (el as HTMLElement).style.pointerEvents = 'none'; });
  await connect(page, 'out-text_input_1-output', 'in-filter_1-input');
  await connect(page, 'out-application_action_1-Action', 'in-filter_1-__action__');
  await expect(page.getByTestId('node-application_action_1')).toContainText('Updates expression');
  await expect(page.getByLabel('Action configuration field')).toHaveValue('expression');
  await expect(page.getByTestId('valid')).toBeVisible({ timeout: 10_000 });
  await page.getByTestId('run').click();
  const app = page.getByRole('dialog', { name: 'Run analytic application' });
  await app.getByLabel('Filter expression').fill('[amount] > 5');
  await app.getByRole('button', { name: 'Run' }).click();
  await expect(page.getByTestId('run-status')).toContainText('succeeded', { timeout: 30_000 });
  await page.getByLabel('Output to display').selectOption('filter_1::true');
  await expect(page.getByTestId('results-grid')).toContainText('8');
  await expect(page.getByTestId('results-grid')).not.toContainText('5');
});

test('validation feedback names the node and the problem', async ({ page }) => {
  await ready(page);
  await addFromPalette(page, 'in-out:text-input');
  const editor = page.getByTestId('text-input-editor');
  await editor.getByRole('button', { name: '+ field' }).click();
  await editor.getByRole('button', { name: '+ row' }).click();
  await addFromPalette(page, 'preparation:formula');
  await connect(page, 'out-text_input_1-output', 'in-formula_1-input');
  await page.getByRole('button', { name: '+ Add formula' }).click();
  await page.locator('#cfg-formula_1-formulas-0-field').fill('x');
  await page.getByTestId('cfg-formula_1-formulas-0-expression-expr').fill('[nope] + 1');
  await expect(page.getByTestId('problem-count')).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId('inspector')).toContainText('Unknown field [nope]');
  await page.getByTestId('cfg-formula_1-formulas-0-expression-expr').fill('');
  await page.locator('.expr .fields button', { hasText: 'Field1' }).click();
  await expect(page.getByTestId('valid')).toBeVisible({ timeout: 10_000 });
});

test('open the example workflow and run it end to end', async ({ page }) => {
  await ready(page);
  await openExample(page);
  await expect(page.getByTestId('node-match_customer')).toBeVisible();
  await expect(page.getByTestId('valid')).toBeVisible({ timeout: 10_000 });
  await page.keyboard.press('F5');
  await expect(page.getByTestId('run-status')).toContainText('succeeded', { timeout: 30_000 });
  await expect(page.getByTestId('rows-match_customer-joined')).toHaveText('7');
  await expect(page.getByTestId('rows-match_customer-left_unjoined')).toHaveText('2');
  await page.getByLabel('Output to display').selectOption('ordered::output');
  await expect(page.getByTestId('result-count')).toContainText('4 rows');
  await expect(page.getByTestId('results-grid')).toContainText('199.98');
  await page.getByTestId('tab-performance').click();
  await expect(page.getByTestId('performance')).toContainText('core.join');
  const written = fs.readFileSync(path.join(REPO, 'examples', 'sales', 'out', 'monthly_sales.csv'), 'utf-8');
  expect(written).toBe(fs.readFileSync(path.join(REPO, 'tests', 'golden', 'monthly_sales', 'monthly_sales.csv'), 'utf-8'));
});

test('undo, redo, grouping, duplicate and delete', async ({ page }) => {
  await ready(page);
  await addFromPalette(page, 'preparation:sort');
  await addFromPalette(page, 'preparation:unique');
  await page.getByTestId('canvas').click({ position: { x: 20, y: 20 } });
  await page.keyboard.press('Control+a');
  await page.keyboard.press('Control+g');
  await expect(page.locator('[data-testid^="container-"]')).toHaveCount(1);
  await page.keyboard.press('Control+z');
  await expect(page.locator('[data-testid^="container-"]')).toHaveCount(0);
  await page.keyboard.press('Control+y');
  await expect(page.locator('[data-testid^="container-"]')).toHaveCount(1);
  await page.getByTestId('node-sort_1').click();
  await page.keyboard.press('Control+d');
  await expect(page.getByTestId('node-sort_2')).toBeVisible();
  await page.keyboard.press('Delete');
  await expect(page.getByTestId('node-sort_2')).toHaveCount(0);
  await page.getByRole('button', { name: 'Undo' }).click();
  await expect(page.getByTestId('node-sort_2')).toBeVisible();
});

test('cancel a long-running workflow', async ({ page }) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowbench-'));
  const wf = path.join(dir, 'slow.flow.json');
  fs.writeFileSync(wf, JSON.stringify({
    format: 'flowengine.workflow', formatVersion: 1, name: 'slow',
    nodes: [
      { id: 'gen', tool: 'core.generate_rows', config: { field: 'n', type: 'int64', init: '1', condition: '[n] <= 3000000', loop: '[n] + 1', max_rows_per_record: 3000001 } },
      { id: 'sq', tool: 'core.formula', config: { formulas: [{ field: 'sq', expression: '[n] * [n]' }] } },
    ],
    edges: [{ from: { node: 'gen', anchor: 'output' }, to: { node: 'sq', anchor: 'input' } }],
  }));
  await ready(page);
  await openExample(page, wf);
  await page.getByTestId('run').click();
  await expect(page.getByTestId('cancel')).toBeEnabled();
  await page.getByTestId('cancel').click();
  await expect(page.getByTestId('run-status')).toContainText('cancelled', { timeout: 60_000 });
});

test('save and reopen preserves nodes, connections, containers and comments', async ({ page }) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowbench-'));
  const target = path.join(dir, 'roundtrip.flow.json');
  await ready(page);
  await openExample(page);
  await page.getByTestId('node-orders').click();
  await page.getByTestId('node-line_values').click({ modifiers: ['Shift'] });
  await page.keyboard.press('Control+g');
  await page.getByRole('toolbar').getByRole('button', { name: 'Comment', exact: true }).click();
  await page.keyboard.press('Control+Shift+s');
  await page.locator('#path-input').fill(target);
  await page.getByTestId('dialog-ok').click();
  await expect(page.getByTestId('toast')).toContainText('Saved');
  const doc = JSON.parse(fs.readFileSync(target, 'utf-8'));
  expect(doc.nodes).toHaveLength(13);
  expect(doc.metadata.ui.containers[0].children.sort()).toEqual(['line_values', 'orders']);
  expect(doc.metadata.ui.comments).toHaveLength(1);
  await page.getByRole('toolbar').getByRole('button', { name: 'New', exact: true }).click();
  await expect(page.getByTestId('node-orders')).toHaveCount(0);
  await openExample(page, target);
  await expect(page.getByTestId('node-orders')).toBeVisible();
  await expect(page.locator('[data-testid^="container-"]')).toHaveCount(1);
  await expect(page.getByTestId('valid')).toBeVisible({ timeout: 10_000 });
});

test('import a .yxmd workflow, see the report, remove the unsupported tool and run', async ({ page }) => {
  await ready(page);
  await openExample(page, path.join(REPO, 'tests', 'fixtures', 'yxmd', 'regional_sales.yxmd'));
  await expect(page.getByTestId('import-report')).toContainText('unsupported');
  await expect(page.getByTestId('problem-count')).toBeVisible({ timeout: 10_000 });
  await expect(page.locator('[data-testid^="container-"]')).toHaveCount(1);
  await page.getByTestId('node-t8').click();
  await page.keyboard.press('Delete');
  await expect(page.getByTestId('valid')).toBeVisible({ timeout: 10_000 });
  await page.getByTestId('run').click();
  await expect(page.getByTestId('run-status')).toContainText('succeeded', { timeout: 30_000 });
  await expect(page.getByTestId('rows-t5-output')).toHaveText('3');
});

test('code tools run only after the user allows them', async ({ page }) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowbench-'));
  const wf = path.join(dir, 'py.flow.json');
  fs.writeFileSync(wf, JSON.stringify({
    format: 'flowengine.workflow', formatVersion: 1, name: 'py',
    nodes: [
      { id: 'src', tool: 'core.text_input', config: { fields: [{ name: 'x', type: 'int32' }], rows: [[2], [3]] } },
      { id: 'py', tool: 'core.python', config: { code: 'from flowbench_io import read_input, write_output\nwrite_output(1, [{"y": r["x"] * 10} for r in read_input(1)])' } },
    ],
    edges: [{ from: { node: 'src', anchor: 'output' }, to: { node: 'py', anchor: 'inputs' } }],
  }));
  await ready(page);
  await openExample(page, wf);
  page.once('dialog', (d) => d.dismiss());
  await page.getByTestId('run').click();
  await expect(page.getByTestId('run-status')).toHaveCount(0);
  page.once('dialog', (d) => d.accept());
  await page.getByTestId('run').click();
  await expect(page.getByTestId('run-status')).toContainText('succeeded', { timeout: 60_000 });
  await expect(page.getByTestId('rows-py-output1')).toHaveText('2');
});

test('database extensions are available and the connection manager opens', async ({ page }) => {
  await ready(page);
  await expect(page.getByTestId('palette-extension-flowengine.macro')).toContainText('Workflow Macro');
  await expect(page.getByTestId('palette-extension-flowengine.batch_macro')).toContainText('Batch Macro');
  await expect(page.getByTestId('palette-extension-flowengine.iterative_macro')).toContainText('Iterative Macro');
  await page.getByTestId('palette-extension-flowengine.macro').dblclick();
  await expect(page.getByTestId('inspector')).toContainText('Workflow Macro');
  await page.getByRole('radio', { name: 'Basic: definition_path' }).click();
  await expect(page.getByTestId('inspector').getByLabel('Definition path')).toBeVisible();
  await expect(page.getByTestId('palette-extension-core.db_input')).toContainText('Database Input');
  await page.getByRole('toolbar').getByRole('button', { name: 'Databases' }).click();
  const dialog = page.getByRole('dialog', { name: 'Database connections' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText('No database connections yet.')).toBeVisible();
  await dialog.getByRole('button', { name: 'New connection' }).click();
  await expect(dialog.getByLabel('Database type')).toHaveValue('postgresql');
  await dialog.getByRole('button', { name: 'Close' }).click();
  await expect(dialog).toHaveCount(0);
  await page.getByTestId('palette-extension-core.db_output').dblclick();
  await expect(page.getByTestId('inspector')).toContainText('Write-enabled connection');
  await expect(page.getByTestId('inspector')).toContainText('Append to existing table');
  await expect(page.getByTestId('inspector')).toContainText('confirmation on every workflow run');
  await page.getByTestId('palette-extension-flowengine.batch_macro').dblclick();
  await expect(page.getByTestId('inspector')).toContainText('Batch Macro');
  await expect(page.getByTestId('inspector').getByLabel('Max iterations')).toBeVisible();
  await page.getByTestId('palette-extension-flowengine.iterative_macro').dblclick();
  await expect(page.getByTestId('inspector')).toContainText('Iterative Macro');
  await expect(page.getByTestId('inspector').getByLabel('Terminate expression')).toBeVisible();
});
