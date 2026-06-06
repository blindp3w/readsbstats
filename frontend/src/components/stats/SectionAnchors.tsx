// xl-only in-page TOC chip row: [Overview] [Activity] [Rankings] [Coverage].
// Active state is driven by IntersectionObserver against the `<section id>`
// elements those chips link to. Hidden below xl per the plan (the page is
// short enough on smaller screens that scrollspy is overkill).

import { useEffect, useState } from 'react';
import { cn } from '@/lib/cn';
import { STATS_SECTIONS, type Section } from './sections';

// Callers may pass a `sections` prop to add/remove entries (e.g. Stats appends a
// gated "VDL2" section); defaults to the shared STATS_SECTIONS.
export function SectionAnchors({ sections = STATS_SECTIONS }: { sections?: Section[] }) {
  // Optional-chain the initial id: an empty `sections` (e.g. all entries gated
  // off) must not throw at construction. Mirrors the `?.` fallback in the
  // activeId derive below (BUG-16).
  const [active, setActive] = useState<string>(sections[0]?.id ?? '');
  // Re-run the observer when the set of section ids changes (e.g. VDL2 toggles).
  const ids = sections.map((s) => s.id).join(',');

  useEffect(() => {
    const els = sections
      .map((s) => document.getElementById(s.id))
      .filter((el): el is HTMLElement => el !== null);
    if (els.length === 0) return;

    const obs = new IntersectionObserver(
      (entries) => {
        // Pick the entry with the smallest positive top — i.e. the
        // currently-visible section closest to the viewport top.
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible.length > 0) setActive(visible[0].target.id);
      },
      {
        // Top margin offsets the sticky nav + range bar (~91 px). Bottom
        // margin shrinks the "visible" band so we activate the section
        // whose heading is in the top half of the viewport.
        rootMargin: '-91px 0px -50% 0px',
        threshold: 0,
      },
    );
    els.forEach((el) => obs.observe(el));
    return () => obs.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-run when the id set changes
  }, [ids]);

  // Derive (don't store) the highlighted id so it can't go stale if the
  // currently-active section is removed (e.g. VDL2 toggles off).
  const activeId = sections.some((s) => s.id === active) ? active : sections[0]?.id;

  return (
    <nav
      aria-label="Statistics sections"
      data-testid="stats-section-anchors"
      className="hidden xl:flex xl:flex-wrap xl:items-center xl:gap-2"
    >
      {sections.map((s) => {
        const isActive = activeId === s.id;
        return (
          <a
            key={s.id}
            href={`#${s.id}`}
            aria-label={`Jump to ${s.label}`}
            aria-current={isActive ? 'true' : undefined}
            className={cn(
              'inline-flex items-center rounded-full border px-3 py-0.5 text-xs font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
              isActive
                ? 'border-[var(--color-accent)] bg-[var(--color-accent)] text-white'
                : 'border-[var(--color-border-default)] text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)]',
            )}
          >
            {s.label}
          </a>
        );
      })}
    </nav>
  );
}
