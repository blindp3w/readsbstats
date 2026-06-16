import { useQuery, useQueryClient } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
import { errMsg } from '@/lib/errMsg';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Table, THead, TBody, TR, TH, TD } from '@/components/ui/Table';
import { Button } from '@/components/ui/Button';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { Badge } from '@/components/ui/Badge';

interface FeederStatus {
  name: string;
  unit: string;
  systemd?: 'active' | 'inactive' | 'failed' | 'unavailable' | string;
  port_status?: 'open' | 'closed' | string;
  overall: 'ok' | 'error' | 'unknown';
  details?: Record<string, string | number | null>;
}

interface FeedersResponse {
  feeders: FeederStatus[];
  has_feeders: boolean;
}

function overallBadge(overall: FeederStatus['overall']) {
  if (overall === 'ok') return <Badge variant="success">OK</Badge>;
  if (overall === 'error') return <Badge variant="danger">error</Badge>;
  return <Badge variant="muted">unknown</Badge>;
}

function systemdBadge(s: FeederStatus['systemd']) {
  if (!s) return <span className="text-[var(--color-text-dim)]">—</span>;
  if (s === 'active') return <Badge variant="success">{s}</Badge>;
  if (s === 'unavailable') return <Badge variant="muted">{s}</Badge>;
  if (s === 'inactive' || s === 'failed') return <Badge variant="danger">{s}</Badge>;
  return <Badge variant="warn">{s}</Badge>;
}

export default function FeedersPage() {
  const qc = useQueryClient();
  const q = useQuery<FeedersResponse>({
    queryKey: ['feeders'],
    queryFn: () => apiJson<FeedersResponse>('feeders'),
    staleTime: 30_000,
  });

  const allUnavailable =
    q.data?.has_feeders &&
    q.data.feeders.length > 0 &&
    q.data.feeders.every((f) => f.systemd === 'unavailable');

  return (
    <div className="mx-auto max-w-5xl space-y-4 px-4 py-6" data-testid="page-feeders">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold">Feeders</h1>
          <p className="text-sm text-[var(--color-text-dim)]">
            ADS-B / MLAT feeder service status.
          </p>
        </div>
        <Button
          variant="secondary"
          onClick={() => qc.invalidateQueries({ queryKey: ['feeders'] })}
          disabled={q.isFetching}
          data-testid="feeders-refresh"
        >
          {q.isFetching ? 'Refreshing…' : 'Refresh'}
        </Button>
      </header>

      {q.isError && <Alert variant="error">Failed to load: {errMsg(q.error)}</Alert>}

      {q.data && !q.data.has_feeders && (
        <Alert variant="info" data-testid="feeders-not-configured">
          No feeders configured. Set <code>RSBS_FEEDERS</code> in the systemd service file to
          monitor third-party feeders.
        </Alert>
      )}

      {allUnavailable && (
        <Alert variant="info" data-testid="feeders-all-unavailable">
          All feeder statuses are <code>unavailable</code> — this is expected when the web server is
          running on a different host than the receiver. Verify the <code>readsbstats</code> user is
          in the <code>systemd-journal</code> group on the Pi.
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Status</CardTitle>
        </CardHeader>
        <CardContent>
          {q.isLoading && <Skeleton className="h-32 w-full" />}
          {q.data && q.data.feeders.length > 0 && (
            <Table data-testid="feeders-table">
              <THead>
                <TR>
                  <TH>Name</TH>
                  <TH className="hidden sm:table-cell">Service</TH>
                  <TH>Systemd</TH>
                  <TH className="hidden sm:table-cell">Port</TH>
                  <TH>Overall</TH>
                </TR>
              </THead>
              <TBody>
                {q.data.feeders.map((f) => (
                  <TR key={f.unit} data-testid={`feeders-row-${f.name}`}>
                    <TD className="font-medium">{f.name}</TD>
                    <TD className="hidden font-mono text-xs sm:table-cell">{f.unit}</TD>
                    <TD>{systemdBadge(f.systemd)}</TD>
                    <TD className="hidden sm:table-cell">
                      {f.port_status ? (
                        f.port_status === 'open' ? (
                          <Badge variant="success">open</Badge>
                        ) : (
                          <Badge variant="danger">{f.port_status}</Badge>
                        )
                      ) : (
                        <span className="text-[var(--color-text-dim)]">—</span>
                      )}
                    </TD>
                    <TD>{overallBadge(f.overall)}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
