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

// jsdom has no WebGL2 context, so a real MapLibre instance would throw on
// init. Globally stub the lazy map wrappers — symmetric with the EChart
// mock above. Affects the Flight smoke test (which lazy-imports RouteMap)
// and any future smoke coverage of Map.tsx (which lazy-imports LiveMap).
vi.mock('@/components/RouteMap', () => ({
  default: () => null,
}));
vi.mock('@/components/LiveMap', () => ({
  default: () => null,
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

// jsdom has no ResizeObserver. The map command bar uses one to track its
// rendered height so MapLibre's native controls can be shifted above the
// bar; in tests we just need a no-op shim so the component doesn't throw.
if (typeof window !== 'undefined' && typeof window.ResizeObserver === 'undefined') {
  class ResizeObserverShim {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).ResizeObserver = ResizeObserverShim;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).ResizeObserver = ResizeObserverShim;
}

// jsdom has no matchMedia. The v2.9.0 useIsMobile hook (and any other
// matchMedia consumer) needs at least a stubbed factory; default to
// `matches: false` (desktop). Individual tests can override via
// `Object.defineProperty(window, 'matchMedia', { value: ... })`.
if (typeof window !== 'undefined' && !window.matchMedia) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  });
}

// jsdom has no IntersectionObserver. The Stats page section anchors use one
// for active-state scrollspy; a no-op shim is enough to let the component
// mount without throwing in tests.
if (typeof window !== 'undefined' && typeof window.IntersectionObserver === 'undefined') {
  class IntersectionObserverShim {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
    takeRecords(): [] {
      return [];
    }
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).IntersectionObserver = IntersectionObserverShim;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).IntersectionObserver = IntersectionObserverShim;
}
