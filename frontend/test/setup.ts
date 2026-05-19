// vitest setup — runs before each test file
import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

// jsdom has no canvas, so an actual ECharts instance would throw on init.
// Globally stub the wrapper so any test that mounts a migrated page
// (Metrics / Stats / Flight / TopChart) renders without crashing. Tests
// that need to inspect EChart's props (top-chart-click) re-mock locally.
vi.mock('@/components/charts/EChart', () => ({
  EChart: () => null,
}));

// jsdom doesn't implement Element.scrollIntoView / hasPointerCapture, which
// Radix Select calls when opening its listbox. Stub them so the dropdown
// can render in test runs.
if (typeof window !== 'undefined') {
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => {};
  }
}
