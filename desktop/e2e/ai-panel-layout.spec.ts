import { expect, test } from '@playwright/test';

test('AI panel keeps its header controls visible and the right workspace resizes by dragging', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto('/');
  await page.getByRole('toolbar').getByRole('button', { name: 'Flowbench AI' }).click();
  const sidebar = page.getByTestId('ai-sidebar');
  const gear = sidebar.getByRole('button', { name: 'AI settings' });
  await expect(gear).toBeInViewport();
  const dock = page.getByRole('region', { name: 'Inspector and AI workspace' });
  const box = (await dock.boundingBox())!;
  expect(box.x + box.width).toBeLessThanOrEqual(1280);
  const gearBox = (await gear.boundingBox())!;
  expect(gearBox.x + gearBox.width).toBeLessThanOrEqual(box.x + box.width);
  await page.screenshot({ path: 'test-results/ai-panel-default.png' });
  const handle = page.getByRole('separator', { name: 'Resize right workspace' });
  const h = (await handle.boundingBox())!;
  await page.mouse.move(h.x + h.width / 2, h.y + 200);
  await page.mouse.down();
  await page.mouse.move(h.x - 300, h.y + 200, { steps: 8 });
  await page.mouse.up();
  const wider = (await dock.boundingBox())!;
  expect(wider.width).toBeGreaterThan(box.width + 250);
  await expect(gear).toBeInViewport();
  await page.screenshot({ path: 'test-results/ai-panel-wide.png' });
  await page.reload();
  await page.getByRole('toolbar').getByRole('button', { name: 'Flowbench AI' }).click();
  expect(Math.abs((await dock.boundingBox())!.width - wider.width)).toBeLessThan(4);
});
