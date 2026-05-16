import { useState } from 'react';
import { NavLink, Link } from 'react-router-dom';
import { cn } from '@/lib/cn';
import { useUnitsStore, type UnitSystem } from '@/store/units';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import { LiveCountBadge } from '@/components/LiveCountBadge';

// Top nav. Mirrors the v1 Jinja nav structure (8 links + brand). The
// hamburger appears below 1024 px (md-) — Tailwind's `md:` breakpoint;
// v1's was 1100px but the React layout is denser so we can collapse
// later. Touch targets: every link is min-h-[44px] on mobile.
const LINKS = [
  { to: '/', label: 'Statistics', end: true },
  { to: '/history', label: 'History' },
  { to: '/map', label: 'Map' },
  { to: '/gallery', label: 'Gallery' },
  { to: '/watchlist', label: 'Watchlist' },
  { to: '/feeders', label: 'Feeders' },
  { to: '/metrics', label: 'Metrics' },
  { to: '/settings', label: 'Settings' },
];

function UnitsSelect() {
  const units = useUnitsStore((s) => s.units);
  const setUnits = useUnitsStore((s) => s.setUnits);
  return (
    <Select value={units} onValueChange={(v) => setUnits(v as UnitSystem)}>
      <SelectTrigger
        data-testid="nav-units-select"
        className="min-h-[36px] w-[130px] text-xs md:min-h-[28px] md:py-0.5"
        aria-label="Units"
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="aeronautical">Aeronautical</SelectItem>
        <SelectItem value="metric">Metric</SelectItem>
        <SelectItem value="imperial">Imperial</SelectItem>
      </SelectContent>
    </Select>
  );
}

export function Nav() {
  const [open, setOpen] = useState(false);
  return (
    <nav
      data-testid="app-nav"
      className="sticky top-0 z-40 border-b border-[var(--color-border-default)] bg-[var(--color-surface)]/85 backdrop-blur supports-[backdrop-filter]:bg-[var(--color-surface)]/70"
    >
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-2.5 md:py-1.5">
        <Link
          to="/"
          className="text-sm font-semibold"
          onClick={() => setOpen(false)}
          data-testid="nav-brand"
        >
          ✈ readsbstats <span className="text-xs text-[var(--color-text-dim)]">v2</span>
        </Link>

        <div className="flex items-center gap-2 md:order-2">
          <LiveCountBadge />
          <UnitsSelect />
          <button
            type="button"
            aria-label="Toggle navigation"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className="rounded p-2 text-base md:hidden focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)] min-h-[44px] min-w-[44px]"
            data-testid="nav-toggle"
          >
            ☰
          </button>
        </div>

        <ul
          className={cn(
            'fixed inset-x-0 top-[57px] flex flex-col gap-1 border-b border-[var(--color-border-default)] bg-[var(--color-surface)] px-4 py-3',
            'md:static md:flex-row md:gap-4 md:border-0 md:bg-transparent md:p-0',
            open ? 'flex' : 'hidden md:flex',
          )}
          data-testid="nav-links"
        >
          {LINKS.map((l) => (
            <li key={l.to}>
              <NavLink
                to={l.to}
                end={l.end}
                onClick={() => setOpen(false)}
                className={({ isActive }) =>
                  cn(
                    // Mobile keeps the 44px touch target; desktop is compact
                    // (height ~28px) to match v1 density.
                    'flex items-center rounded px-2.5 py-2 text-sm font-medium transition-colors min-h-[44px]',
                    'hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
                    'md:min-h-0 md:px-2 md:py-0.5',
                    isActive
                      ? 'text-[var(--color-text)] md:border-b-2 md:border-[var(--color-accent)] md:rounded-b-none bg-[var(--color-surface-2)] md:bg-transparent'
                      : 'text-[var(--color-text-dim)]',
                  )
                }
                data-testid={`nav-${l.label.toLowerCase()}`}
              >
                {l.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </div>
    </nav>
  );
}
