import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { apiJson } from '@/lib/api';
import { cn } from '@/lib/cn';

// V1 parity: polls /api/live every 15 s and shows the active-aircraft count
// in the nav. A green dot means the receiver is actively tracking aircraft;
// a dim grey dot means zero aircraft or the poll hasn't returned yet.
//
// Clicking the badge goes to /map so users can investigate "is my receiver
// alive?" with one tap.

interface LiveResponse {
  count?: number;
  now?: number;
}

export function LiveCountBadge() {
  const q = useQuery<LiveResponse>({
    queryKey: ['live-nav'],
    queryFn: () => apiJson<LiveResponse>('live'),
    refetchInterval: 15_000,
    refetchIntervalInBackground: false,
    staleTime: 10_000,
    retry: 1,
  });

  const count = q.data?.count;
  const ts = q.data?.now;
  const isActive = count != null && count > 0;
  const title = q.isError
    ? `Live poll failed: ${(q.error as Error).message}`
    : ts
      ? `Active aircraft — updated ${new Date(ts * 1000).toLocaleTimeString()}`
      : 'Active aircraft';

  return (
    <Link
      to="/map"
      title={title}
      aria-label={title}
      data-testid="nav-live-badge"
      className={cn(
        'inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs font-medium tabnum',
        'min-h-[36px] md:min-h-[28px]',
        'border-[var(--color-border-default)] hover:bg-[var(--color-surface-2)] transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'inline-block h-2 w-2 rounded-full',
          isActive
            ? 'bg-[var(--color-success)] shadow-[0_0_6px_var(--color-success)]'
            : 'bg-[var(--color-text-dim)]',
          q.isLoading && !q.data && 'animate-pulse',
        )}
      />
      <span className={cn(isActive ? 'text-[var(--color-text)]' : 'text-[var(--color-text-dim)]')}>
        {count ?? '—'}
      </span>
    </Link>
  );
}
