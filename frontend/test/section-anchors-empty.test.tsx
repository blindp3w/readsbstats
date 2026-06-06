/**
 * BUG-16: SectionAnchors initialised its active-id state with
 * `useState(sections[0].id)` — an unguarded index that throws when `sections`
 * is empty. (The later fallback at the activeId derive already used `?.`.) The
 * fix mirrors that with `sections[0]?.id ?? ''`.
 */
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { SectionAnchors } from '@/components/stats/SectionAnchors';

describe('SectionAnchors with empty sections (BUG-16)', () => {
  it('renders without throwing when sections is empty', () => {
    expect(() => render(<SectionAnchors sections={[]} />)).not.toThrow();
  });

  it('renders the nav (with no chips) for an empty section list', () => {
    const { getByTestId, container } = render(<SectionAnchors sections={[]} />);
    expect(getByTestId('stats-section-anchors')).toBeTruthy();
    expect(container.querySelectorAll('a').length).toBe(0);
  });
});
