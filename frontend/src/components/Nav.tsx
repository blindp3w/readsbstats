import { useState } from 'react';
import { NavLink, Link, useLocation } from 'react-router-dom';
import { HamburgerMenuIcon, CaretDownIcon } from '@radix-ui/react-icons';
import { cn } from '@/lib/cn';
import { useVdl2Enabled } from '@/hooks/useVdl2Enabled';
import { useUnitsStore, type UnitSystem } from '@/store/units';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/Tooltip';
import { LiveCountBadge } from '@/components/LiveCountBadge';

// Unit-system → (label, units) for the Nav units selector tooltip + dropdown
// subtitles. Single source of truth so the desktop tooltip and the touch
// fallback (subtitle inside each SelectItem) can't drift apart.
const UNIT_OPTIONS: { value: UnitSystem; label: string; units: string }[] = [
  { value: 'aeronautical', label: 'Aeronautical', units: 'nm · ft · kts' },
  { value: 'metric', label: 'Metric', units: 'km · m · km/h' },
  { value: 'imperial', label: 'Imperial', units: 'mi · ft · mph' },
];

// Top nav. Mirrors the v1 Jinja nav structure (8 links + brand). The
// mobile menu is a Radix DropdownMenu (below md/720 px under the
// project's 15 px html font-size); desktop is a flat horizontal list.
//
// Sprint 1 #1 (M10.1): at md and lg viewports (720–1199 px) the last
// four items collapse into a `More ▾` dropdown so the row fits on
// iPad portrait. At xl (≥1200 px) all eight render inline. The
// constant arrays below are the single source of truth used by the
// inline desktop list, the More dropdown, and the mobile hamburger.
interface NavItem {
  to: string;
  label: string;
  end?: boolean;
}

const INLINE_LINKS: NavItem[] = [
  { to: '/', label: 'Statistics', end: true },
  { to: '/history', label: 'History' },
  { to: '/map', label: 'Map' },
  { to: '/gallery', label: 'Gallery' },
];

const OVERFLOW_LINKS: NavItem[] = [
  { to: '/watchlist', label: 'Watchlist' },
  { to: '/feeders', label: 'Feeders' },
  { to: '/metrics', label: 'Metrics' },
  { to: '/settings', label: 'Settings' },
];

// Opt-in VDL2/ACARS tab. Inserted into the overflow set only when the backend
// reports the feature is enabled (RSBS_VDL2_ENABLED). Kept before Settings so
// Settings stays last. See internal_docs/features/vdl2-ui-integration.md.
const VDL2_LINK: NavItem = { to: '/vdl2', label: 'VDL2' };

// Reads the capability flag from the shared ['settings'] query (App.tsx seeds
// the same key on boot, so this adds no extra request) and returns the link
// arrays to render. When VDL2 is disabled the lists are identical to before.
function useNavLinks(): { inlineLinks: NavItem[]; overflowLinks: NavItem[] } {
  const vdl2Enabled = useVdl2Enabled();
  let overflowLinks = OVERFLOW_LINKS;
  if (vdl2Enabled) {
    // Insert VDL2 immediately before Settings (found by route, not a magic
    // index) so Settings stays last even if OVERFLOW_LINKS is reordered.
    const i = OVERFLOW_LINKS.findIndex((l) => l.to === '/settings');
    const at = i === -1 ? OVERFLOW_LINKS.length : i;
    overflowLinks = [...OVERFLOW_LINKS.slice(0, at), VDL2_LINK, ...OVERFLOW_LINKS.slice(at)];
  }
  return { inlineLinks: INLINE_LINKS, overflowLinks };
}

function UnitsSelect() {
  const units = useUnitsStore((s) => s.units);
  const setUnits = useUnitsStore((s) => s.setUnits);
  // Controlled tooltip state so it auto-closes when the Select opens.
  // Radix Tooltip + Select composition isn't documented; the default
  // tooltip stays visible on hover/focus and the click that opens the
  // Select doesn't move focus away (focus moves into Select.Content's
  // portal). Without coordination the tooltip lingers over the open
  // dropdown.
  const [tipOpen, setTipOpen] = useState(false);
  const [selectOpen, setSelectOpen] = useState(false);
  return (
    <Select
      value={units}
      onValueChange={(v) => setUnits(v as UnitSystem)}
      onOpenChange={(o) => {
        setSelectOpen(o);
        // Force-close the tooltip when the Select closes — otherwise
        // Radix Tooltip's internal hover state (still active because the
        // trigger element never lost the pointer) re-fires it the moment
        // we release the open-gate, producing a brief flicker on every
        // dropdown dismissal.
        if (!o) setTipOpen(false);
      }}
    >
      <Tooltip open={tipOpen && !selectOpen} onOpenChange={setTipOpen} delayDuration={300}>
        <TooltipTrigger asChild>
          <SelectTrigger
            data-testid="nav-units-select"
            className="min-h-[36px] w-[130px] text-xs md:min-h-[28px] md:py-0.5"
            aria-label="Units"
          >
            <SelectValue />
          </SelectTrigger>
        </TooltipTrigger>
        <TooltipContent data-testid="nav-units-tooltip" className="w-[200px] p-2">
          <div className="space-y-1 text-xs" role="presentation">
            {UNIT_OPTIONS.map((o) => (
              <div
                key={o.value}
                className={cn(
                  'flex items-baseline justify-between gap-3',
                  o.value === units
                    ? 'text-[var(--color-text)] font-semibold'
                    : 'text-[var(--color-text-dim)]',
                )}
              >
                <span>{o.label}</span>
                <span className="tabnum font-mono">{o.units}</span>
              </div>
            ))}
          </div>
        </TooltipContent>
      </Tooltip>
      <SelectContent>
        {UNIT_OPTIONS.map((o) => (
          <SelectItem key={o.value} value={o.value} subtitle={o.units}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

// Sprint 1 #1: `More ▾` overflow dropdown for items 5-8 at md and lg
// viewports. The trigger inherits the inline-link styling (same
// `border-b-2 border-accent` underline when the current route belongs
// to the overflow set) so it reads as a peer of the inline NavLinks
// rather than a generic button.
function MoreNavMenu({ overflowLinks }: { overflowLinks: NavItem[] }) {
  const { pathname } = useLocation();
  const isActive = overflowLinks.some((l) => l.to === pathname);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-haspopup="menu"
          aria-label="More navigation items"
          data-testid="nav-more-trigger"
          className={cn(
            'inline-flex items-center gap-1 rounded px-2 py-0.5 text-sm font-medium transition-colors',
            'hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
            isActive
              ? 'text-[var(--color-text)] border-b-2 border-[var(--color-accent)] rounded-b-none'
              : 'text-[var(--color-text-dim)]',
          )}
        >
          More
          <CaretDownIcon width={14} height={14} aria-hidden="true" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent data-testid="nav-more-content" className="min-w-[180px]">
        {overflowLinks.map((l) => (
          <DropdownMenuItem key={l.to} asChild>
            <NavLink
              to={l.to}
              className={({ isActive: itemActive }) =>
                cn(
                  'block w-full',
                  itemActive && 'text-[var(--color-text)] bg-[var(--color-surface-2)]',
                )
              }
              data-testid={`nav-more-item-${l.label.toLowerCase()}`}
            >
              {l.label}
            </NavLink>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function MobileNavMenu({ links }: { links: NavItem[] }) {
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
        {links.map((l) => (
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
  const { inlineLinks, overflowLinks } = useNavLinks();
  return (
    <nav
      data-testid="app-nav"
      // z-[1000]: the sticky nav must sit above the map (MapLibre's canvas
      // and controls live below 1000) so the /flight Route map doesn't
      // bleed through the translucent backdrop on iOS scroll. Held at 1000
      // to keep margin over any future map-overlay UI.
      // bg /95 (fallback) and /85 (with backdrop-blur) lift opacity enough
      // that the basemap can't show through during scroll.
      // pt-[env(safe-area-inset-top)] clears the iPhone Dynamic Island /
      // notch on notched devices; resolves to 0 on desktop so behaviour
      // is unchanged there. Paired with --rsbs-nav-h in index.css which
      // includes the same env() term so the Stats sticky range bar (and
      // any future docked chrome) align with the nav's bottom edge.
      className="sticky top-0 z-[1000] border-b border-[var(--color-border-default)] bg-[var(--color-surface)]/95 pt-[env(safe-area-inset-top)] backdrop-blur supports-[backdrop-filter]:bg-[var(--color-surface)]/85"
    >
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-2.5 md:py-1.5">
        <Link to="/" className="text-sm font-semibold" data-testid="nav-brand">
          ✈ readsbstats <span className="text-xs text-[var(--color-text-dim)]">v2</span>
        </Link>

        <div className="flex items-center gap-2 md:order-2">
          <LiveCountBadge />
          <UnitsSelect />
          <MobileNavMenu links={[...inlineLinks, ...overflowLinks]} />
        </div>

        {/* Desktop horizontal nav — hidden below md. The mobile flow uses
            the DropdownMenu above instead of toggling this list.
            Sprint 1 #1 (M10.1): the last 4 links are hidden at <xl via
            `xl:list-item hidden` on their <li>s, and `<MoreNavMenu>`
            fills the slot at md/lg via `xl:hidden`. At xl (≥1200 px)
            all 8 items render inline; the More button is hidden. */}
        <ul
          className="hidden md:flex md:flex-row md:items-center md:gap-4"
          data-testid="nav-links-desktop"
        >
          {inlineLinks.map((l) => (
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
          {overflowLinks.map((l) => (
            <li key={l.to} className="hidden xl:list-item">
              <NavLink
                to={l.to}
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
          <li className="xl:hidden">
            <MoreNavMenu overflowLinks={overflowLinks} />
          </li>
        </ul>
      </div>
    </nav>
  );
}
