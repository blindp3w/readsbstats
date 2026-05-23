import { describe, it, expect } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';

import { aircraftIconSvg, getIconType } from '@/lib/aircraftIcon';
import { FLAG_MILITARY } from '@/lib/flags';

// audit-12 #176 — track rotation used to live in a CSS transform string
// inside this module, which required numeric coercion to be safe. With the
// MapLibre migration (v2.4), rotation is handled by Marker's typed
// `rotation` prop and no longer touches this module. The string-template
// surface is gone by construction. What stays load-bearing here is the
// military fill/stroke discrimination and getIconType()'s shape selection.

describe('aircraftIconSvg', () => {
  it('renders a non-military aircraft with the default white fill', () => {
    const html = renderToStaticMarkup(aircraftIconSvg(0, 'jet'));
    expect(html).toContain('fill="#ffffff"');
    expect(html).toContain('stroke="#1d4ed8"');
    expect(html).not.toContain('#ef4444');
  });

  it('renders a military aircraft with the red fill', () => {
    const html = renderToStaticMarkup(aircraftIconSvg(FLAG_MILITARY, 'jet'));
    expect(html).toContain('fill="#ef4444"');
    expect(html).toContain('stroke="#7f1d1d"');
  });

  it('preserves the military fill when other flags are also set', () => {
    const html = renderToStaticMarkup(aircraftIconSvg(FLAG_MILITARY | 0x4, 'heli'));
    expect(html).toContain('fill="#ef4444"');
  });

  it('handles null / undefined flags as non-military', () => {
    expect(renderToStaticMarkup(aircraftIconSvg(null, 'jet'))).toContain('fill="#ffffff"');
    expect(renderToStaticMarkup(aircraftIconSvg(undefined, 'jet'))).toContain('fill="#ffffff"');
  });

  it('picks the viewBox by iconType', () => {
    expect(renderToStaticMarkup(aircraftIconSvg(0, 'jet'))).toContain('viewBox="-1 -2 34 34"');
    expect(renderToStaticMarkup(aircraftIconSvg(0, 'heli'))).toContain('viewBox="-13 -13 90 90"');
    expect(renderToStaticMarkup(aircraftIconSvg(0, 'glider'))).toContain('viewBox="-5.8 -10 76 76"');
  });

  it('does not embed any rotate() string — rotation is the Marker prop now', () => {
    const html = renderToStaticMarkup(aircraftIconSvg(0, 'jet'));
    expect(html).not.toMatch(/rotate\(/);
    expect(html).not.toMatch(/transform/);
  });
});

describe('getIconType', () => {
  it('returns the mapped icon for an ADS-B category', () => {
    expect(getIconType('A2', null)).toBe('jet');
    expect(getIconType('A7', null)).toBe('heli');
    expect(getIconType('B1', null)).toBe('glider');
  });

  it('falls back to helicopter regex on aircraftType', () => {
    expect(getIconType(null, 'EC35')).toBe('heli');
  });

  it('falls back to light regex on aircraftType', () => {
    expect(getIconType(null, 'C172')).toBe('light');
  });

  it('defaults to jet for unknown', () => {
    expect(getIconType(null, null)).toBe('jet');
    expect(getIconType(undefined, undefined)).toBe('jet');
    expect(getIconType('Z9', 'XYZ')).toBe('jet');
  });
});
