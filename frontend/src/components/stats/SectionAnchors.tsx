// xl-only in-page TOC chip row: [Overview] [Activity] [Rankings] [Coverage].
// Active state is driven by IntersectionObserver against the `<section id>`
// elements those chips link to. Hidden below xl per the plan (the page is
// short enough on smaller screens that scrollspy is overkill).

import { useEffect, useState } from 'react';
import { cn } from '@/lib/cn';

interface Section {
  id: string;
  label: string;
}

// Module-local (not exported) so the file's only export is the component
// itself — `react-refresh/only-export-components` hygiene. Nothing
// outside this file references SECTIONS.
const SECTIONS: Section[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'activity', label: 'Activity' },
  { id: 'rankings', label: 'Rankings' },
  { id: 'coverage', label: 'Coverage' },
];

export function SectionAnchors() {
  const [active, setActive] = useState<string>(SECTIONS[0].id);

  useEffect(() => {
    const els = SECTIONS.map((s) => document.getElementById(s.id)).filter(
      (el): el is HTMLElement => el !== null,
    );
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
  }, []);

  return (
    <nav
      aria-label="Statistics sections"
      data-testid="stats-section-anchors"
      className="hidden xl:flex xl:flex-wrap xl:items-center xl:gap-2"
    >
      {SECTIONS.map((s) => {
        const isActive = active === s.id;
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
