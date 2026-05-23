import { LayersIcon } from '@radix-ui/react-icons';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover';
import { cn } from '@/lib/cn';

interface Props {
  // Layer states + setters.
  showHeatmap: boolean;
  onToggleHeatmap: () => void;
  heatmapLoading?: boolean;
  showCoverage: boolean;
  onToggleCoverage: () => void;
  coverageLoading?: boolean;
  sidebarOpen: boolean;
  onToggleSidebar: () => void;
  // Layout variant. `inline` renders 3 pills in a row (used at lg+);
  // `popover` collapses to a single icon trigger (used at <lg and <sm).
  variant: 'inline' | 'popover';
}

// Layer toggles for the live map. Same data flow as the old inline UI — each
// toggle drives a parent useState. At smaller widths the three toggles fold
// into a Popover (side="top" so it doesn't open into the bottom of the screen)
// to keep the command bar's Row 1 on a single line.
export function MapLayersControl({
  showHeatmap,
  onToggleHeatmap,
  heatmapLoading,
  showCoverage,
  onToggleCoverage,
  coverageLoading,
  sidebarOpen,
  onToggleSidebar,
  variant,
}: Props) {
  const items = (
    <>
      <LayerToggle
        testid="map-toggle-heatmap"
        label="Heatmap"
        active={showHeatmap}
        loading={heatmapLoading}
        onClick={onToggleHeatmap}
      />
      <LayerToggle
        testid="map-toggle-coverage"
        label="Coverage"
        active={showCoverage}
        loading={coverageLoading}
        onClick={onToggleCoverage}
      />
      <LayerToggle
        testid="map-toggle-list"
        label="List"
        active={sidebarOpen}
        onClick={onToggleSidebar}
      />
    </>
  );

  if (variant === 'inline') {
    return (
      <div className="flex items-center gap-1" data-testid="map-layers-inline">
        {items}
      </div>
    );
  }

  const activeCount = (showHeatmap ? 1 : 0) + (showCoverage ? 1 : 0) + (sidebarOpen ? 1 : 0);

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="Layers"
          data-testid="map-layers-popover-trigger"
          className={cn(
            'relative inline-flex h-9 w-9 items-center justify-center rounded border transition-colors',
            'border-[var(--color-border-default)] bg-[var(--color-surface)]',
            'hover:bg-[var(--color-surface-2)]',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
            activeCount > 0 && 'text-[var(--color-accent)]',
          )}
        >
          <LayersIcon aria-hidden="true" />
          {activeCount > 0 && (
            <span
              aria-hidden="true"
              data-testid="map-layers-active-count"
              className="absolute -right-1 -top-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--color-accent)] px-1 text-[10px] font-semibold text-white"
            >
              {activeCount}
            </span>
          )}
          <span className="sr-only">
            {activeCount === 0
              ? 'No layers active'
              : `${activeCount} layer${activeCount === 1 ? '' : 's'} active`}
          </span>
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="start"
        collisionPadding={16}
        className="flex flex-col gap-1 p-2"
        data-testid="map-layers-popover"
      >
        {items}
      </PopoverContent>
    </Popover>
  );
}

function LayerToggle({
  label,
  active,
  loading,
  onClick,
  testid,
}: {
  label: string;
  active: boolean;
  loading?: boolean;
  onClick: () => void;
  testid: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      data-testid={testid}
      className={cn(
        'inline-flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-medium transition-colors min-h-[28px]',
        active
          ? 'bg-[var(--color-accent)] text-white'
          : 'text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]',
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'inline-block h-1.5 w-1.5 rounded-full',
          active ? 'bg-white' : 'bg-[var(--color-text-dim)]',
          loading && 'animate-pulse',
        )}
      />
      {label}
    </button>
  );
}
