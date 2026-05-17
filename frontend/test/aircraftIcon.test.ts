import { describe, it, expect, vi } from 'vitest';

// Stub Leaflet — we only inspect the `html` field the icon factory produces.
vi.mock('leaflet', () => {
  return {
    default: {
      divIcon: (opts: { html: string }) => opts,
    },
  };
});

import { aircraftIcon, getIconType } from '@/lib/aircraftIcon';

describe('aircraftIcon — track coercion (audit-12 #176)', () => {
  it('numeric track renders as degrees in transform attribute', () => {
    const icon = aircraftIcon(45, 0, 'jet') as unknown as { html: string };
    expect(icon.html).toContain('rotate(45deg)');
  });

  it('null track defaults to 0deg', () => {
    const icon = aircraftIcon(null, 0, 'jet') as unknown as { html: string };
    expect(icon.html).toContain('rotate(0deg)');
  });

  it('undefined track defaults to 0deg', () => {
    const icon = aircraftIcon(undefined, 0, 'jet') as unknown as { html: string };
    expect(icon.html).toContain('rotate(0deg)');
  });

  it('coerces a string-typed track to a numeric degrees value', () => {
    // Hostile / drift scenario: API drift sends a string for `track`.
    // The previous code passed it straight into the style attribute, so
    // a value like "0;}<script>alert(1)</script>" would break out of the
    // CSS context. Today: hard-coerced to a number, with NaN → 0.
    const icon = aircraftIcon('0;}<script>x</script>' as unknown as number, 0, 'jet') as unknown as { html: string };
    expect(icon.html).toContain('rotate(0deg)');
    expect(icon.html).not.toContain('<script');
  });

  it('coerces NaN track to 0deg', () => {
    const icon = aircraftIcon(Number.NaN, 0, 'jet') as unknown as { html: string };
    expect(icon.html).toContain('rotate(0deg)');
  });

  it('rounds fractional degrees to integers', () => {
    const icon = aircraftIcon(123.456, 0, 'jet') as unknown as { html: string };
    expect(icon.html).toContain('rotate(123deg)');
    expect(icon.html).not.toContain('123.456');
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
