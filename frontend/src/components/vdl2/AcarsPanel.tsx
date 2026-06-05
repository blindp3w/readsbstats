import { useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { MessageList } from '@/components/vdl2/MessageList';
import { useVdl2FlightWindow } from '@/hooks/useVdl2Enabled';
import type { Vdl2MessagesResponse } from '@/lib/types';

// Opt-in: ACARS messages received during one flight, shown on the flight-detail
// page. Renders nothing unless VDL2 is available. The slack-widened flight window
// (to catch gate/OOOI traffic) + availability gate come from useVdl2FlightWindow.

interface Props {
  icao: string;
  firstSeen: number;
  lastSeen: number;
  // Where the panel is mounted. Adjusts the empty-state wording and the test
  // ids so the same component reads correctly on the flight page (one flight's
  // window) and the aircraft page (the airframe's whole history). Defaults to
  // 'flight' for backward compatibility.
  context?: 'flight' | 'aircraft';
}

export function AcarsPanel({ icao, firstSeen, lastSeen, context = 'flight' }: Props) {
  const { available, since, until } = useVdl2FlightWindow(firstSeen, lastSeen);
  const q = useQuery({
    queryKey: ['vdl2-flight', icao, since, until],
    enabled: available && !!icao,
    queryFn: () => {
      const p = new URLSearchParams({
        since: String(since),
        until: String(until),
        limit: '100',
      });
      return apiJson<Vdl2MessagesResponse>(`vdl2/messages/${icao}?${p.toString()}`);
    },
    staleTime: 30_000,
  });

  if (!available) return null;

  const messages = q.data?.messages ?? [];
  // limit=100; next_before_id non-null means more exist beyond the first page.
  const countLabel = q.data ? `${messages.length}${q.data.next_before_id != null ? '+' : ''}` : '…';
  const emptyText =
    context === 'aircraft'
      ? 'No ACARS messages recorded for this aircraft.'
      : 'No ACARS messages for this flight.';
  return (
    <Card data-testid={`${context}-acars-card`}>
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
            data-testid={`${context}-acars-empty`}
          >
            {emptyText}
          </p>
        )}
        {messages.length > 0 && (
          // Cap the height and scroll, matching the position log, so a chatty
          // flight's ACARS log doesn't push the rest of the page far down.
          <div className="max-h-[480px] overflow-y-auto" data-testid={`${context}-acars-scroll`}>
            <MessageList messages={messages} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
