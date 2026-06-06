import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { MessageList } from '@/components/vdl2/MessageList';
import { useVdl2FlightMessages } from '@/hooks/useVdl2Enabled';

// Opt-in: ACARS messages received during one flight, shown on the flight-detail
// page. Renders nothing unless VDL2 is available. The slack-widened flight window
// (to catch gate/OOOI traffic), availability gate, and message query all come
// from useVdl2FlightMessages — the same hook the flight header's ACARS badge
// uses, so the badge and this block can never disagree (one deduped request).

interface Props {
  icao: string;
  firstSeen: number;
  lastSeen: number;
  // Where the panel is mounted. Adjusts the test ids so the same component reads
  // correctly on the flight page (one flight's window) and the aircraft page
  // (the airframe's whole history). Defaults to 'flight'.
  context?: 'flight' | 'aircraft';
}

export function AcarsPanel({ icao, firstSeen, lastSeen, context = 'flight' }: Props) {
  const { available, isLoading, isSuccess, isError, error, messages, hasMore } =
    useVdl2FlightMessages(icao, firstSeen, lastSeen);

  if (!available) return null;
  // UX: when the flight/airframe has no ACARS, hide the block entirely rather
  // than showing an empty "ACARS (0)" card.
  if (isSuccess && messages.length === 0) return null;

  // limit=100; hasMore (next_before_id non-null) means more exist beyond page 1.
  const countLabel = isLoading ? '…' : `${messages.length}${hasMore ? '+' : ''}`;
  return (
    <Card data-testid={`${context}-acars-card`}>
      <CardHeader>
        <CardTitle>ACARS ({countLabel})</CardTitle>
      </CardHeader>
      <CardContent>
        {isError && <Alert variant="error">Failed to load ACARS: {(error as Error).message}</Alert>}
        {isLoading && <Skeleton className="h-24 w-full" />}
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
