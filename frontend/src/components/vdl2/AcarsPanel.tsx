import { useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { MessageList } from '@/components/vdl2/MessageList';
import { useVdl2Enabled } from '@/hooks/useVdl2Enabled';
import type { Vdl2MessagesResponse } from '@/lib/types';

// Opt-in: ACARS messages received during one flight, shown on the flight-detail
// page. Renders nothing unless RSBS_VDL2_ENABLED. Queries the per-aircraft VDL2
// endpoint scoped to the flight's [first_seen, last_seen] window (± SLACK to
// catch OOOI traffic at the gate / after landing).
const SLACK_SEC = 1800;

interface Props {
  icao: string;
  firstSeen: number;
  lastSeen: number;
}

export function AcarsPanel({ icao, firstSeen, lastSeen }: Props) {
  const enabled = useVdl2Enabled();
  const q = useQuery({
    queryKey: ['vdl2-flight', icao, firstSeen, lastSeen],
    enabled: enabled && !!icao,
    queryFn: () => {
      const p = new URLSearchParams({
        since: String(firstSeen - SLACK_SEC),
        until: String(lastSeen + SLACK_SEC),
        limit: '100',
      });
      return apiJson<Vdl2MessagesResponse>(`vdl2/messages/${icao}?${p.toString()}`);
    },
    staleTime: 30_000,
  });

  if (!enabled) return null;

  const messages = q.data?.messages ?? [];
  // limit=100; next_before_id non-null means more exist beyond the first page.
  const countLabel = q.data ? `${messages.length}${q.data.next_before_id != null ? '+' : ''}` : '…';
  return (
    <Card data-testid="flight-acars-card">
      <CardHeader>
        <CardTitle>ACARS ({countLabel})</CardTitle>
      </CardHeader>
      <CardContent>
        {q.isError && (
          <Alert variant="error">Failed to load ACARS: {(q.error as Error).message}</Alert>
        )}
        {q.isLoading && <Skeleton className="h-24 w-full" />}
        {q.isSuccess && messages.length === 0 && (
          <p
            className="py-4 text-center text-sm text-[var(--color-text-dim)]"
            data-testid="flight-acars-empty"
          >
            No ACARS messages for this flight.
          </p>
        )}
        {messages.length > 0 && <MessageList messages={messages} />}
      </CardContent>
    </Card>
  );
}
