import { expect, test } from '@playwright/test';
import * as path from 'node:path';

// Produces docs/images/flowbench-example.png (documentation screenshot of a real run).
test('documentation screenshot of the example workflow after a run', async ({ page }) => {
  const repo = path.resolve(process.cwd(), '..');
  await page.goto('/');
  await expect(page.getByText(/engine \d/)).toBeVisible();
  await page.getByRole('toolbar').getByRole('button', { name: 'Open', exact: true }).click();
  await page.locator('#path-input').fill(path.join(repo, 'examples', 'sales', 'monthly_sales.flow.json'));
  await page.getByTestId('dialog-ok').click();
  await expect(page.getByTestId('valid')).toBeVisible({ timeout: 10_000 });
  await page.keyboard.press('F5');
  await expect(page.getByTestId('run-status')).toContainText('succeeded', { timeout: 30_000 });
  await page.getByTestId('node-match_customer').click();
  await page.getByLabel('Output to display').selectOption('ordered::output');
  await page.screenshot({ path: path.join(repo, 'docs', 'images', 'flowbench-example.png') });
});
