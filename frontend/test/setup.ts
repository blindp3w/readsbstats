// vitest setup — runs before each test file
import '@testing-library/jest-dom/vitest';

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
