import { NavLink, Link } from 'react-router-dom';
import { HamburgerMenuIcon } from '@radix-ui/react-icons';
import { cn } from '@/lib/cn';
import { useUnitsStore, type UnitSystem } from '@/store/units';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu';
import { LiveCountBadge } from '@/components/LiveCountBadge';

// Top nav. Mirrors the v1 Jinja nav structure (8 links + brand). The
// mobile menu is a Radix DropdownMenu (below md/1024px); desktop is a
// flat horizontal list. Touch targets: every link is min-h-[44px] on mobile.
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

function MobileNavMenu() {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label="Open navigation menu"
          className="inline-flex items-center justify-center rounded p-2 md:hidden focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)] min-h-[44px] min-w-[44px]"
          data-testid="nav-toggle"
        >
          <HamburgerMenuIcon width={18} height={18} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent data-testid="nav-links" className="min-w-[220px]">
        {LINKS.map((l) => (
          <DropdownMenuItem key={l.to} asChild>
            <NavLink
              to={l.to}
              end={l.end}
              className={({ isActive }) =>
                cn(
                  // The base DropdownMenu.Item classes from the wrapper are
                  // forwarded via asChild/Slot; layer the active state on top.
                  'block w-full',
                  isActive && 'text-[var(--color-text)] bg-[var(--color-surface-2)]',
                )
              }
              data-testid={`nav-${l.label.toLowerCase()}`}
            >
              {l.label}
            </NavLink>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function Nav() {
  return (
    <nav
      data-testid="app-nav"
      // z-[1000]: Leaflet's max pane z-index is 800 (.leaflet-control). The
      // sticky nav must sit above it so the /flight Route map doesn't bleed
      // through the translucent backdrop on iOS.
      // bg /95 (fallback) and /85 (with backdrop-blur) lift opacity enough
      // that the satellite tiles can't show through during scroll.
      className="sticky top-0 z-[1000] border-b border-[var(--color-border-default)] bg-[var(--color-surface)]/95 backdrop-blur supports-[backdrop-filter]:bg-[var(--color-surface)]/85"
    >
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-2.5 md:py-1.5">
        <Link to="/" className="text-sm font-semibold" data-testid="nav-brand">
          ✈ readsbstats <span className="text-xs text-[var(--color-text-dim)]">v2</span>
        </Link>

        <div className="flex items-center gap-2 md:order-2">
          <LiveCountBadge />
          <UnitsSelect />
          <MobileNavMenu />
        </div>

        {/* Desktop horizontal nav — hidden below md. The mobile flow uses
            the DropdownMenu above instead of toggling this list. */}
        <ul
          className="hidden md:flex md:flex-row md:items-center md:gap-4"
          data-testid="nav-links-desktop"
        >
          {LINKS.map((l) => (
            <li key={l.to}>
              <NavLink
                to={l.to}
                end={l.end}
                className={({ isActive }) =>
                  cn(
                    'flex items-center rounded px-2 py-0.5 text-sm font-medium transition-colors',
                    'hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
                    isActive
                      ? 'text-[var(--color-text)] border-b-2 border-[var(--color-accent)] rounded-b-none'
                      : 'text-[var(--color-text-dim)]',
                  )
                }
                data-testid={`nav-desktop-${l.label.toLowerCase()}`}
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
