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

export function SourceBadge({ source }: { source: string | null | undefined }) {
  if (!source) return null;
  const s = source.toLowerCase();
  if (s.startsWith('adsb')) return <Badge variant="success">ADS-B</Badge>;
  if (s === 'mlat') return <Badge variant="warn">MLAT</Badge>;
  if (s === 'mixed') return <Badge variant="default">mixed</Badge>;
  return <Badge variant="muted">{source}</Badge>;
}
