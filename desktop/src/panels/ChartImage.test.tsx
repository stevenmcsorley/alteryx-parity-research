import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { ChartImage, isChartSvg } from './ResultsPanel';
import { chartFileStem, chartTitle } from './ChartViewer';

const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"><title>Mean &amp; CI</title><rect width="10" height="10" fill="#fff"/></svg>';

describe('chart cells', () => {
  afterEach(cleanup);

  it('recognises engine SVG documents only', () => {
    expect(isChartSvg(svg)).toBe(true);
    expect(isChartSvg('<svg onload="x">')).toBe(false);
    expect(isChartSvg('plain text')).toBe(false);
  });

  it('renders the chart as an image data URL, never as inline markup', () => {
    const { container } = render(<ChartImage svg={svg} />);
    const img = screen.getByRole('img') as HTMLImageElement;
    expect(img.src.startsWith('data:image/svg+xml;charset=utf-8,')).toBe(true);
    expect(decodeURIComponent(img.src.split(',')[1])).toBe(svg);
    expect(container.querySelector('svg')).toBeNull();
  });

  it('decodes the title for captions and file names', () => {
    expect(chartTitle(svg)).toBe('Mean & CI');
    expect(chartFileStem(svg)).toBe('Mean_CI');
  });

  it('opens a zoomable viewer that fits, zooms and closes', () => {
    render(<ChartImage svg={svg} />);
    fireEvent.click(screen.getByRole('button', { name: 'Enlarge Mean & CI' }));
    const viewer = screen.getByRole('dialog', { name: 'Chart: Mean & CI' });
    const stage = screen.getByTestId('chart-stage');
    Object.defineProperty(stage, 'clientWidth', { value: 824 });
    Object.defineProperty(stage, 'clientHeight', { value: 424 });
    fireEvent.click(screen.getByRole('button', { name: /^Fit/ }));
    expect(viewer.textContent).toContain('200%');  // 800×400 usable area fits the 400×200 chart at 2×
    const big = viewer.querySelector('img') as HTMLImageElement;
    expect(big.style.width).toBe('800px');
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    expect(viewer.textContent).toContain('250%');
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    expect(viewer.textContent).toContain('160%');
    fireEvent.click(screen.getByRole('button', { name: /^100%/ }));
    expect(big.style.width).toBe('400px');
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('downloads in the browser build', async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    render(<ChartImage svg={svg} />);
    fireEvent.click(screen.getByRole('button', { name: 'Save as SVG' }));
    await vi.waitFor(() => expect(click).toHaveBeenCalled());
    click.mockRestore();
  });
});
