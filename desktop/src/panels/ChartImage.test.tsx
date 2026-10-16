import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { ChartImage, isChartSvg } from './ResultsPanel';

const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><title>Mean &amp; CI</title><rect width="10" height="10" fill="#fff"/></svg>';

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
});
