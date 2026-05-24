import { primaryFlagLabel } from '@/lib/flags';
import { Badge } from '@/components/ui/Badge';

// Renders the highest-precedence flag for a flight. Precedence matches
// notifier and v1 templates: military > interesting > anonymous.
export function FlagBadge({ flags }: { flags: number | null | undefined }) {
  const label = primaryFlagLabel(flags);
  if (!label) return null;
  if (label === 'military') return <Badge variant="success">mil</Badge>;
  if (label === 'interesting') return <Badge variant="warn">int</Badge>;
  if (label === 'anonymous') return <Badge variant="danger">anon</Badge>;
  return null;
}

interface SourceBadgeProps {
  source: string | null | undefined;
  // 'md' (default): existing Badge styling. 'sm' shrinks padding + font
  // for dense table cells where a left-border stripe already carries the
  // primary signal (v2.8.0 M8.4).
  size?: 'sm' | 'md';
}

export function SourceBadge({ source, size = 'md' }: SourceBadgeProps) {
  if (!source) return null;
  const s = source.toLowerCase();
  // 10 px is the smallest size that stays sharp across browsers'
  // anti-aliasing; the brief's "9 px" gets fuzzy on retina + dark bg.
  const cls = size === 'sm' ? 'px-1.5 py-0 text-[10px]' : undefined;
  if (s.startsWith('adsb'))
    return (
      <Badge variant="success" className={cls}>
        ADS-B
      </Badge>
    );
  if (s === 'mlat')
    return (
      <Badge variant="warn" className={cls}>
        MLAT
      </Badge>
    );
  if (s === 'mixed')
    return (
      <Badge variant="default" className={cls}>
        mixed
      </Badge>
    );
  return (
    <Badge variant="muted" className={cls}>
      {source}
    </Badge>
  );
}
