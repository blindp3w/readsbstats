import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { apiFetch, apiJson, ApiError } from '@/lib/api';
import { errMsg } from '@/lib/errMsg';
import type {
  WatchlistEntry,
  WatchlistMatchType as MatchType,
  WatchlistResponse,
} from '@/lib/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Table, THead, TBody, TR, TH, TD } from '@/components/ui/Table';
import { Input } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import { Button } from '@/components/ui/Button';
import { Skeleton } from '@/components/ui/Skeleton';
import { fmtDate } from '@/lib/format';
import { Alert } from '@/components/ui/Alert';
import { Badge } from '@/components/ui/Badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog';

// Watchlist CRUD page.
//
// Pinned constants from database.py (must NOT drift — they're enforced both
// server-side via Pydantic Field(max_length=...) and client-side here so
// the user sees a clear error before a network roundtrip).
const VALUE_MAX = 64; // database.WATCHLIST_VALUE_MAX
const LABEL_MAX = 255; // database.WATCHLIST_LABEL_MAX

const MATCH_TYPE_LABEL: Record<MatchType, string> = {
  icao: 'ICAO hex',
  registration: 'Registration',
  callsign_prefix: 'Callsign prefix',
};

const VALUE_PLACEHOLDER: Record<MatchType, string> = {
  icao: 'e.g. 3c4b17',
  registration: 'e.g. SP-LRF',
  callsign_prefix: 'e.g. LOT',
};

export default function WatchlistPage() {
  const qc = useQueryClient();
  const [matchType, setMatchType] = useState<MatchType>('icao');
  const [value, setValue] = useState('');
  const [label, setLabel] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<WatchlistEntry | null>(null);

  const list = useQuery<WatchlistResponse>({
    queryKey: ['watchlist'],
    queryFn: () => apiJson<WatchlistResponse>('watchlist'),
    staleTime: 5_000,
  });

  const addMut = useMutation<
    WatchlistEntry,
    Error,
    { match_type: MatchType; value: string; label: string | null }
  >({
    mutationFn: async (entry) =>
      apiJson<WatchlistEntry>('watchlist', {
        method: 'POST',
        body: JSON.stringify(entry),
        headers: { 'Content-Type': 'application/json' },
      }),
    onSuccess: () => {
      setValue('');
      setLabel('');
      setFormError(null);
      toast.success('Added to watchlist');
      qc.invalidateQueries({ queryKey: ['watchlist'] });
    },
    onError: (err) => {
      const msg = describeMutationError(err);
      setFormError(msg);
      toast.error(msg);
    },
  });

  // Audit-12 #160 — declare the optimistic-context shape via the 4th
  // useMutation generic so onError sees `ctx` as typed `DelMutCtx`
  // instead of relying on a hand-typed annotation that would silently
  // go stale if onMutate's return shape changed.
  type DelMutCtx = { prev?: WatchlistResponse };
  const delMut = useMutation<void, Error, number, DelMutCtx>({
    mutationFn: async (id) => {
      await apiFetch(`watchlist/${id}`, { method: 'DELETE' });
    },
    onMutate: async (id): Promise<DelMutCtx> => {
      // Optimistic — remove from the list immediately.
      await qc.cancelQueries({ queryKey: ['watchlist'] });
      const prev = qc.getQueryData<WatchlistResponse>(['watchlist']);
      if (prev) {
        qc.setQueryData<WatchlistResponse>(['watchlist'], {
          entries: prev.entries.filter((e) => e.id !== id),
        });
      }
      return { prev };
    },
    onError: (err, _id, ctx) => {
      if (ctx?.prev) qc.setQueryData(['watchlist'], ctx.prev);
      toast.error(`Delete failed: ${err.message}`);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['watchlist'] });
    },
    onSuccess: () => {
      toast.success('Removed');
    },
  });

  function onSubmit(e: React.SyntheticEvent<HTMLFormElement>) {
    e.preventDefault();
    const trimmedValue = value.trim();
    const trimmedLabel = label.trim();
    if (!trimmedValue) {
      setFormError('Value is required.');
      return;
    }
    if (matchType === 'icao' && !/^[0-9a-fA-F]{6}$/.test(trimmedValue)) {
      setFormError('ICAO hex must be exactly 6 hexadecimal characters (e.g. 3c4b17).');
      return;
    }
    if (trimmedValue.length > VALUE_MAX) {
      setFormError(`Value too long (max ${VALUE_MAX} characters).`);
      return;
    }
    if (trimmedLabel.length > LABEL_MAX) {
      setFormError(`Label too long (max ${LABEL_MAX} characters).`);
      return;
    }
    addMut.mutate({
      match_type: matchType,
      value: trimmedValue,
      label: trimmedLabel || null,
    });
  }

  return (
    <div className="mx-auto max-w-5xl space-y-4 px-4 py-6" data-testid="page-watchlist">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold">Watchlist</h1>
        <p className="text-sm text-[var(--color-text-dim)]">
          Aircraft you want first-sighting alerts for. ICAO hex / registration / callsign prefix
          matching.
        </p>
      </header>

      <Card data-testid="watchlist-add-card">
        <CardContent className="pt-3 pb-3">
          <form
            onSubmit={onSubmit}
            className="flex flex-wrap items-end gap-2 md:flex-nowrap"
            data-testid="watchlist-add-form"
          >
            <div className="w-[160px] shrink-0">
              <Label htmlFor="match-type">Match</Label>
              <Select value={matchType} onValueChange={(v) => setMatchType(v as MatchType)}>
                <SelectTrigger
                  id="match-type"
                  data-testid="watchlist-match-type"
                  className="md:min-h-[36px] md:py-1.5"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(MATCH_TYPE_LABEL) as MatchType[]).map((t) => (
                    <SelectItem key={t} value={t}>
                      {MATCH_TYPE_LABEL[t]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="min-w-[180px] flex-1">
              <Label htmlFor="value">Value</Label>
              <Input
                id="value"
                type="text"
                value={value}
                onChange={(e) => setValue(e.target.value)}
                placeholder={VALUE_PLACEHOLDER[matchType]}
                autoComplete="off"
                spellCheck={false}
                maxLength={VALUE_MAX + 16}
                data-testid="watchlist-value"
              />
            </div>
            <div className="min-w-[160px] flex-1">
              <Label htmlFor="label">Label (optional)</Label>
              <Input
                id="label"
                type="text"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="Label"
                maxLength={LABEL_MAX + 16}
                data-testid="watchlist-label"
              />
            </div>
            <Button
              type="submit"
              size="field"
              disabled={addMut.isPending}
              data-testid="watchlist-add-submit"
              className="shrink-0 self-end"
            >
              {addMut.isPending ? 'Adding…' : 'Add'}
            </Button>
          </form>
          {formError && (
            <div className="mt-2">
              <Alert variant="error" data-testid="watchlist-form-error">
                {formError}
              </Alert>
            </div>
          )}
        </CardContent>
      </Card>

      <Card data-testid="watchlist-entries-card">
        <CardHeader>
          <CardTitle>Entries</CardTitle>
        </CardHeader>
        <CardContent>
          {list.isError && <Alert variant="error">Failed to load: {errMsg(list.error)}</Alert>}
          {list.isLoading && <Skeleton className="h-24 w-full" />}
          {list.data && list.data.entries.length === 0 && (
            <p
              className="py-6 text-center text-sm text-[var(--color-text-dim)]"
              data-testid="watchlist-empty"
            >
              No entries yet. Use the form above to add one.
            </p>
          )}
          {list.data && list.data.entries.length > 0 && (
            <Table data-testid="watchlist-table">
              <THead>
                <TR>
                  <TH>Type</TH>
                  <TH>Value</TH>
                  <TH>Label</TH>
                  <TH className="hidden sm:table-cell">Added</TH>
                  <TH>Status</TH>
                  <TH>
                    <span className="sr-only">Actions</span>
                  </TH>
                </TR>
              </THead>
              <TBody>
                {list.data.entries.map((e) => (
                  <TR key={e.id} data-testid={`watchlist-row-${e.id}`}>
                    <TD>{MATCH_TYPE_LABEL[e.match_type] ?? e.match_type}</TD>
                    <TD className="font-mono tabnum">{e.value}</TD>
                    <TD>{e.label ?? '—'}</TD>
                    <TD className="hidden text-xs text-[var(--color-text-dim)] tabnum sm:table-cell">
                      {fmtDate(e.created_at)}
                    </TD>
                    <TD>
                      {e.airborne ? (
                        <Badge variant="success">airborne</Badge>
                      ) : (
                        <Badge variant="muted">—</Badge>
                      )}
                    </TD>
                    <TD className="text-right">
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => setPendingDelete(e)}
                        disabled={delMut.isPending}
                        data-testid={`watchlist-delete-${e.id}`}
                        aria-label={`Remove ${e.value}`}
                      >
                        Remove
                      </Button>
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={!!pendingDelete}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
      >
        <DialogContent data-testid="watchlist-delete-dialog">
          <DialogHeader>
            <DialogTitle>Remove watchlist entry?</DialogTitle>
            <DialogDescription>
              {pendingDelete ? (
                <>
                  This will stop first-sighting alerts for{' '}
                  <span className="font-mono text-[var(--color-text)]">{pendingDelete.value}</span>
                  {pendingDelete.label ? ` (${pendingDelete.label})` : ''}.
                </>
              ) : null}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setPendingDelete(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                if (pendingDelete) delMut.mutate(pendingDelete.id);
                setPendingDelete(null);
              }}
              data-testid="watchlist-delete-confirm"
            >
              Remove
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function describeMutationError(err: Error): string {
  // ApiError.body is JSON-encoded from FastAPI; extract `detail` if we can.
  // Plain Error.message looks like "HTTP 409 Conflict"; that's already useful.
  // Audit 17: narrow with `instanceof ApiError` instead of an `as any` cast —
  // apiFetch throws ApiError, which exposes the typed `body`.
  const body = err instanceof ApiError ? err.body : undefined;
  if (body) {
    try {
      const parsed = JSON.parse(body) as { detail?: string };
      if (parsed.detail) return parsed.detail;
    } catch {
      /* not JSON */
    }
  }
  return err.message;
}
