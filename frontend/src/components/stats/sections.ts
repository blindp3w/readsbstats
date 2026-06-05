// Stats page in-page TOC sections. Shared by Stats.tsx (which owns the matching
// <section id> elements and may append a gated VDL2 entry) and SectionAnchors
// (the scrollspy chip row). Kept in its own module so neither a component file
// owns the list (avoids react-refresh non-component-export churn).

export interface Section {
  id: string;
  label: string;
}

export const STATS_SECTIONS: Section[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'activity', label: 'Activity' },
  { id: 'rankings', label: 'Rankings' },
  { id: 'coverage', label: 'Coverage' },
];
