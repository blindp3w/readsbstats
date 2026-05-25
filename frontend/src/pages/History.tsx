import { useEffect, useMemo, useRef, useState } from 'react';
import { CaretDownIcon, Cross2Icon, PlusIcon } from '@radix-ui/react-icons';
import { useQuery } from '@tanstack/react-query';
import { apiJson, apiUrl } from '@/lib/api';
import { useSearchParam, useSearchParamBatch } from '@/hooks/useSearchParam';
import { cn } from '@/lib/cn';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Input } from '@/components/ui/Input';
import { DatePicker } from '@/components/ui/DatePicker';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select';
import { Label } from '@/components/ui/Label';
import { Button, buttonClass } from '@/components/ui/Button';
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/Popover';
import { FlightsTable, type Flight, type SortKey, type SortDir } from '@/components/FlightsTable';
import { Pagination } from '@/components/Pagination';

interface FlightsResponse {
  total: number;
  limit: number;
  offset: number;
  flights: Flight[];
}

const PAGE_SIZE = 100;
// Radix Select forbids "" as an Item value; use this sentinel for "no
// filter" in the source/flags dropdowns and translate at the boundary.
const ANY_VALUE = '__any__';

const SORT_KEYS: SortKey[] = [
  'first_seen',
  'icao_hex',
  'callsign',
  'registration',
  'aircraft_type',
  'primary_source',
  'duration_sec',
  'max_alt_baro',
  'max_gs',
  'max_distance_nm',
  'total_positions',
  'origin_icao',
];

function isSortKey(s: string): s is SortKey {
  return (SORT_KEYS as string[]).includes(s);
}

function localMidnightEpoch(dateStr: string): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr);
  if (!m) return 0;
  return Math.floor(new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).getTime() / 1000);
}

// Single source of truth for the Source / Flag dropdown options. Used by
// the Advanced form Select AND by the chip-display label renderer so
// `?source=adsb` shows as "Source: ADS-B" in the chip, not "Source: adsb".
const SOURCE_OPTIONS: { value: string; label: string }[] = [
  { value: 'adsb', label: 'ADS-B' },
  { value: 'mlat', label: 'MLAT' },
  { value: 'mixed', label: 'mixed' },
  { value: 'other', label: 'other' },
];

const FLAG_OPTIONS: { value: string; label: string }[] = [
  { value: 'military', label: 'military' },
  { value: 'interesting', label: 'interesting' },
  { value: 'anonymous', label: 'anonymous' },
];

function labelFromMap(map: { value: string; label: string }[], v: string): string {
  return map.find((o) => o.value === v)?.label ?? v;
}

// Filter field metadata — drives both the chip row and the popover
// field picker. Single source of truth so adding a field is a one-stop
// edit. URL param assumed single-valued; each field maps to one URL
// param (date range is the only multi-param field, handled specially).
type FieldKey =
  | 'icao'
  | 'callsign'
  | 'registration'
  | 'aircraft_type'
  | 'source'
  | 'flags'
  | 'squawk'
  | 'date';

interface FieldDef {
  key: FieldKey;
  label: string;
}

const FIELD_DEFS: FieldDef[] = [
  { key: 'icao', label: 'ICAO' },
  { key: 'callsign', label: 'Callsign' },
  { key: 'registration', label: 'Registration' },
  { key: 'aircraft_type', label: 'Type' },
  { key: 'source', label: 'Source' },
  { key: 'flags', label: 'Flag' },
  { key: 'squawk', label: 'Squawk' },
  { key: 'date', label: 'Date range' },
];

export default function HistoryPage() {
  // URL-state — all filters / sort / page survive refresh + back button.
  const [dateFrom, setDateFrom] = useSearchParam('date_from', '');
  const [dateTo, setDateTo] = useSearchParam('date_to', '');
  const [icao, setIcao] = useSearchParam('icao', '');
  const [callsign, setCallsign] = useSearchParam('callsign', '');
  const [registration, setRegistration] = useSearchParam('registration', '');
  const [aircraftType, setAircraftType] = useSearchParam('aircraft_type', '');
  const [source, setSource] = useSearchParam('source', '');
  const [flags, setFlags] = useSearchParam('flags', '');
  const [squawk, setSquawk] = useSearchParam('squawk', '');
  const [sortByRaw] = useSearchParam('sort_by', 'first_seen');
  const [sortDirRaw] = useSearchParam('sort_dir', 'desc');
  const [offset, setOffset] = useSearchParam('offset', 0);
  const update = useSearchParamBatch();

  const sortBy: SortKey = isSortKey(String(sortByRaw)) ? (sortByRaw as SortKey) : 'first_seen';
  const sortDir: SortDir = String(sortDirRaw) === 'asc' ? 'asc' : 'desc';

  const queryParams = new URLSearchParams();
  if (dateFrom) queryParams.set('from', String(localMidnightEpoch(String(dateFrom))));
  if (dateTo) queryParams.set('to', String(localMidnightEpoch(String(dateTo)) + 86400));
  if (icao) queryParams.set('icao', String(icao));
  if (callsign) queryParams.set('callsign', String(callsign));
  if (registration) queryParams.set('registration', String(registration));
  if (aircraftType) queryParams.set('aircraft_type', String(aircraftType));
  if (source) queryParams.set('source', String(source));
  if (flags) queryParams.set('flags', String(flags));
  if (squawk) queryParams.set('squawk', String(squawk));
  queryParams.set('sort_by', sortBy);
  queryParams.set('sort_dir', sortDir);
  queryParams.set('limit', String(PAGE_SIZE));
  queryParams.set('offset', String(offset));
  const qs = queryParams.toString();

  const q = useQuery<FlightsResponse>({
    queryKey: ['flights', qs],
    queryFn: () => apiJson<FlightsResponse>(`flights?${qs}`),
    placeholderData: (prev) => prev,
  });

  // Multi-param updates go through the batch helper so they commit atomically.
  // (useSearchParam's per-param setter is stale-reads-prone when chained — see
  //  hooks/useSearchParam.ts.)
  const onSortChange = (key: SortKey, dir: SortDir) => {
    update({ sort_by: key, sort_dir: dir, offset: 0 });
  };

  const resetFilters = () => {
    update({
      date_from: null,
      date_to: null,
      icao: null,
      callsign: null,
      registration: null,
      aircraft_type: null,
      source: null,
      flags: null,
      squawk: null,
      offset: 0,
    });
  };

  // CSV export inherits the current filters; sort/limit/offset aren't useful in CSV
  const exportQs = new URLSearchParams();
  for (const [k, v] of queryParams.entries()) {
    if (k === 'limit' || k === 'offset') continue;
    exportQs.set(k, v);
  }

  // ---- M8.3 chip + Advanced state ---------------------------------------
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const addFilterTriggerRef = useRef<HTMLButtonElement>(null);

  // `/` keyboard shortcut — focuses the + filter… trigger. Skips when an
  // input / textarea / contenteditable is focused (so typing `/` in a
  // text field doesn't hijack) or any modifier is held.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key !== '/') return;
      if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
      const t = e.target as HTMLElement | null;
      if (!t) return;
      const tag = t.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || t.isContentEditable) return;
      e.preventDefault();
      addFilterTriggerRef.current?.click();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, []);

  // Derive chips from URL params each render. Cheap (≤8 params); React
  // Compiler handles memoisation if needed.
  const dateFromStr = String(dateFrom);
  const dateToStr = String(dateTo);
  const activeChips = useMemo(() => {
    const out: { field: FieldKey; label: string; value: string }[] = [];
    if (dateFromStr || dateToStr) {
      out.push({
        field: 'date',
        label: 'Date',
        value: formatDateRange(dateFromStr, dateToStr),
      });
    }
    if (icao) out.push({ field: 'icao', label: 'ICAO', value: String(icao) });
    if (callsign) {
      out.push({ field: 'callsign', label: 'Callsign', value: String(callsign).toUpperCase() });
    }
    if (registration) {
      out.push({
        field: 'registration',
        label: 'Reg',
        value: String(registration).toUpperCase(),
      });
    }
    if (aircraftType) {
      out.push({
        field: 'aircraft_type',
        label: 'Type',
        value: String(aircraftType).toUpperCase(),
      });
    }
    if (source) {
      out.push({
        field: 'source',
        label: 'Source',
        value: labelFromMap(SOURCE_OPTIONS, String(source)),
      });
    }
    if (flags) {
      out.push({ field: 'flags', label: 'Flag', value: labelFromMap(FLAG_OPTIONS, String(flags)) });
    }
    if (squawk) out.push({ field: 'squawk', label: 'Squawk', value: String(squawk) });
    return out;
  }, [dateFromStr, dateToStr, icao, callsign, registration, aircraftType, source, flags, squawk]);

  const activeFieldKeys = new Set(activeChips.map((c) => c.field));
  const anyActive = activeChips.length > 0;

  const removeChip = (field: FieldKey) => {
    if (field === 'date') {
      update({ date_from: null, date_to: null, offset: 0 });
    } else {
      update({ [field]: null, offset: 0 });
    }
  };

  const addFilter = (field: FieldKey, value: string | { from: string; to: string }) => {
    if (field === 'date' && typeof value === 'object') {
      update({
        date_from: value.from || null,
        date_to: value.to || null,
        offset: 0,
      });
    } else if (typeof value === 'string') {
      update({ [field]: value || null, offset: 0 });
    }
  };

  return (
    <div className="mx-auto max-w-7xl space-y-4 px-4 py-6" data-testid="page-history">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold">Flight History</h1>
        <p className="text-sm text-[var(--color-text-dim)]">
          {q.data
            ? `${q.data.total.toLocaleString()} flights match the current filters.`
            : 'Search and filter recorded flights.'}
        </p>
      </header>

      {/* M8.3 + M10.5: chip row pulled out of the filters Card and
          wrapped in a sticky `-mx-4 px-4` bleed that docks under the
          nav as the user scrolls. Same pattern as Stats RangePicker
          (v2.6.0). */}
      <div
        className="sticky z-20 -mx-4 border-b border-[var(--color-border-default)] bg-[var(--color-surface)]/85 px-4 py-2 backdrop-blur supports-[backdrop-filter]:bg-[var(--color-surface)]/70"
        style={{ top: 'var(--rsbs-nav-h, 41px)' }}
        data-testid="history-filter-sticky"
      >
        <div className="flex flex-wrap items-center gap-2">
          {activeChips.map((c) => (
            <FilterChip
              key={c.field}
              field={c.field}
              label={c.label}
              value={c.value}
              onRemove={() => removeChip(c.field)}
            />
          ))}
          <AddFilterPopover
            triggerRef={addFilterTriggerRef}
            activeFieldKeys={activeFieldKeys}
            onAdd={addFilter}
          />
          <a
            href={apiUrl(`flights/export.csv?${exportQs.toString()}`)}
            className={cn(buttonClass('secondary', 'sm'), 'ml-auto')}
            data-testid="history-export-csv"
          >
            Export CSV
          </a>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--color-text-dim)]">
          <span>{q.data ? q.data.total.toLocaleString() : '—'} flights match</span>
          {/* Toggle-button semantics via aria-pressed + data-state. A
              standalone `@radix-ui/react-toggle` would just wrap these
              same attributes around a <button>, so we set them directly
              and skip the extra dep. data-state drives both the
              chevron rotation and the open-state colour fill. */}
          <button
            type="button"
            onClick={() => setAdvancedOpen((s) => !s)}
            aria-pressed={advancedOpen}
            aria-expanded={advancedOpen}
            data-state={advancedOpen ? 'on' : 'off'}
            data-testid="history-advanced-trigger"
            className={cn(
              'inline-flex min-h-[28px] items-center gap-1 rounded-full border px-3 text-xs font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
              advancedOpen
                ? 'border-[var(--color-accent)] bg-[var(--color-accent)]/15 text-[var(--color-accent)]'
                : 'border-[var(--color-border-default)] text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]',
            )}
          >
            <CaretDownIcon
              width={12}
              height={12}
              aria-hidden="true"
              className="transition-transform duration-150 data-[state=on]:rotate-180"
              data-state={advancedOpen ? 'on' : 'off'}
            />
            Advanced
          </button>
          {anyActive && (
            <button
              type="button"
              onClick={resetFilters}
              data-testid="history-reset"
              className="rounded px-1 hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
            >
              Clear all
            </button>
          )}
        </div>
      </div>

      {advancedOpen && (
        <Card data-testid="history-filters-card">
          <CardContent className="pt-3 pb-3">
            <form
              className="grid gap-2 sm:grid-cols-2 md:grid-cols-4 lg:grid-cols-6"
              onSubmit={(e) => {
                e.preventDefault();
                setOffset(0);
              }}
              data-testid="history-filters-form"
            >
              <Field label="From" htmlFor="f-date-from">
                <DatePicker
                  id="f-date-from"
                  value={String(dateFrom)}
                  onChange={(v) => setDateFrom(v)}
                  ariaLabel="From date"
                  data-testid="history-filter-date-from"
                />
              </Field>
              <Field label="To" htmlFor="f-date-to">
                <DatePicker
                  id="f-date-to"
                  value={String(dateTo)}
                  onChange={(v) => setDateTo(v)}
                  ariaLabel="To date"
                  data-testid="history-filter-date-to"
                />
              </Field>
              <Field label="ICAO hex" htmlFor="f-icao">
                <Input
                  id="f-icao"
                  type="text"
                  value={String(icao)}
                  onChange={(e) => setIcao(e.target.value)}
                  placeholder="3c4b17"
                  autoCorrect="off"
                  autoCapitalize="off"
                  data-testid="history-filter-icao"
                />
              </Field>
              <Field label="Callsign" htmlFor="f-callsign">
                <Input
                  id="f-callsign"
                  type="text"
                  value={String(callsign)}
                  onChange={(e) => setCallsign(e.target.value)}
                  placeholder="LOT"
                  data-testid="history-filter-callsign"
                />
              </Field>
              <Field label="Registration" htmlFor="f-reg">
                <Input
                  id="f-reg"
                  type="text"
                  value={String(registration)}
                  onChange={(e) => setRegistration(e.target.value)}
                  placeholder="SP-LRF"
                  data-testid="history-filter-registration"
                />
              </Field>
              <Field label="Aircraft type" htmlFor="f-type">
                <Input
                  id="f-type"
                  type="text"
                  value={String(aircraftType)}
                  onChange={(e) => setAircraftType(e.target.value)}
                  placeholder="B738"
                  data-testid="history-filter-type"
                />
              </Field>
              <Field label="Source" htmlFor="f-source">
                {/* Radix Select.Item rejects "" as a value at runtime, so the
                    "any" option uses sentinel ANY_VALUE and we translate at
                    the boundary. The URL param stays empty for "no filter". */}
                <Select
                  value={String(source) || ANY_VALUE}
                  onValueChange={(v) => setSource(v === ANY_VALUE ? '' : v)}
                >
                  <SelectTrigger
                    id="f-source"
                    data-testid="history-filter-source"
                    className="text-sm"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ANY_VALUE}>any</SelectItem>
                    {SOURCE_OPTIONS.map((o) => (
                      <SelectItem key={o.value} value={o.value}>
                        {o.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
              <Field label="Flag" htmlFor="f-flags">
                <Select
                  value={String(flags) || ANY_VALUE}
                  onValueChange={(v) => setFlags(v === ANY_VALUE ? '' : v)}
                >
                  <SelectTrigger
                    id="f-flags"
                    data-testid="history-filter-flags"
                    className="text-sm"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ANY_VALUE}>any</SelectItem>
                    {FLAG_OPTIONS.map((o) => (
                      <SelectItem key={o.value} value={o.value}>
                        {o.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
              <Field label="Squawk" htmlFor="f-squawk">
                <Input
                  id="f-squawk"
                  type="text"
                  value={String(squawk)}
                  onChange={(e) => setSquawk(e.target.value)}
                  placeholder="7700"
                  inputMode="numeric"
                  data-testid="history-filter-squawk"
                />
              </Field>
              <div className="flex items-end gap-2 sm:col-span-2 md:col-span-4 lg:col-span-1 lg:justify-end">
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={resetFilters}
                  data-testid="history-reset-advanced"
                >
                  Reset
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Results</CardTitle>
        </CardHeader>
        <CardContent>
          <FlightsTable
            flights={q.data?.flights}
            isLoading={q.isLoading}
            error={q.isError ? (q.error as Error) : null}
            sortBy={sortBy}
            sortDir={sortDir}
            onSortChange={onSortChange}
          />
          {q.data && q.data.total > PAGE_SIZE && (
            <Pagination
              total={q.data.total}
              limit={PAGE_SIZE}
              offset={Number(offset)}
              onOffsetChange={setOffset}
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// FilterChip — single pill with label, value, and × button.
// Visual reference: IsolationPills (rounded-full border, 36/44 px tap
// target). Chip body is non-interactive in v1; only × removes.
// ---------------------------------------------------------------------------
function FilterChip({
  field,
  label,
  value,
  onRemove,
}: {
  field: FieldKey;
  label: string;
  value: string;
  onRemove: () => void;
}) {
  return (
    <span
      data-testid={`history-chip-${field}`}
      className="inline-flex min-h-[36px] items-center gap-1.5 rounded-full border border-[var(--color-border-default)] bg-[var(--color-surface-2)]/60 px-3 text-xs"
    >
      <span className="text-[var(--color-text-dim)]">{label}:</span>
      <span className="tabnum text-[var(--color-text)]">{value}</span>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${label} filter`}
        data-testid={`history-chip-${field}-remove`}
        className="ml-0.5 rounded-full p-0.5 text-[var(--color-text-dim)] hover:bg-[var(--color-surface-3)] hover:text-[var(--color-text)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
      >
        <Cross2Icon width={12} height={12} aria-hidden="true" />
      </button>
    </span>
  );
}

// ---------------------------------------------------------------------------
// AddFilterPopover — two-step picker: field list → value input → submit.
// ---------------------------------------------------------------------------
function AddFilterPopover({
  triggerRef,
  activeFieldKeys,
  onAdd,
}: {
  triggerRef: React.RefObject<HTMLButtonElement | null>;
  activeFieldKeys: Set<FieldKey>;
  onAdd: (field: FieldKey, value: string | { from: string; to: string }) => void;
}) {
  const [open, setOpen] = useState(false);
  const [pickedField, setPickedField] = useState<FieldKey | null>(null);
  const [textValue, setTextValue] = useState('');
  const [selectValue, setSelectValue] = useState<string>('');
  const [dateFromValue, setDateFromValue] = useState('');
  const [dateToValue, setDateToValue] = useState('');

  // Reset step + value buffers each time the popover opens.
  useEffect(() => {
    if (!open) {
      setPickedField(null);
      setTextValue('');
      setSelectValue('');
      setDateFromValue('');
      setDateToValue('');
    }
  }, [open]);

  const availableFields = FIELD_DEFS.filter((f) => !activeFieldKeys.has(f.key));

  const submit = () => {
    if (!pickedField) return;
    if (pickedField === 'date') {
      if (!dateFromValue && !dateToValue) return;
      onAdd('date', { from: dateFromValue, to: dateToValue });
    } else if (pickedField === 'source' || pickedField === 'flags') {
      if (!selectValue) return;
      onAdd(pickedField, selectValue);
    } else {
      if (!textValue.trim()) return;
      onAdd(pickedField, textValue.trim());
    }
    setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          ref={triggerRef}
          type="button"
          data-testid="history-add-filter-trigger"
          className="inline-flex min-h-[36px] items-center gap-1 rounded-full border border-dashed border-[var(--color-border-default)] px-3 text-xs text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
        >
          <PlusIcon width={12} height={12} aria-hidden="true" />
          filter…
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-[min(320px,calc(100vw-2rem))] p-2"
        data-testid="history-add-filter-content"
      >
        {pickedField == null ? (
          // Step 1 — field picker.
          <div className="flex flex-col">
            {availableFields.length === 0 && (
              <p className="px-2 py-3 text-center text-xs text-[var(--color-text-dim)]">
                All filters in use. Remove one to add another.
              </p>
            )}
            {availableFields.map((f) => (
              <button
                key={f.key}
                type="button"
                onClick={() => setPickedField(f.key)}
                data-testid={`history-add-filter-field-${f.key}`}
                className="rounded px-2 py-2 text-left text-sm hover:bg-[var(--color-surface-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
              >
                {f.label}
              </button>
            ))}
          </div>
        ) : (
          // Step 2 — value input for the picked field.
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between text-xs">
              <button
                type="button"
                onClick={() => setPickedField(null)}
                className="rounded px-1 text-[var(--color-text-dim)] hover:text-[var(--color-text)]"
              >
                ← back
              </button>
              <span className="font-medium">
                {FIELD_DEFS.find((f) => f.key === pickedField)?.label}
              </span>
            </div>
            {pickedField === 'date' ? (
              <div className="space-y-2">
                <DatePicker
                  value={dateFromValue}
                  onChange={(v) => setDateFromValue(v)}
                  ariaLabel="From date"
                />
                <DatePicker
                  value={dateToValue}
                  onChange={(v) => setDateToValue(v)}
                  ariaLabel="To date"
                />
              </div>
            ) : pickedField === 'source' || pickedField === 'flags' ? (
              <Select value={selectValue} onValueChange={setSelectValue}>
                <SelectTrigger className="text-sm">
                  <SelectValue placeholder="Pick a value…" />
                </SelectTrigger>
                <SelectContent>
                  {(pickedField === 'source' ? SOURCE_OPTIONS : FLAG_OPTIONS).map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <Input
                autoFocus
                value={textValue}
                onChange={(e) => setTextValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    submit();
                  }
                }}
                placeholder={placeholderFor(pickedField)}
                data-testid="history-add-filter-value-input"
              />
            )}
            <div className="flex justify-end">
              <Button type="button" size="sm" onClick={submit}>
                Add
              </Button>
            </div>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}

function placeholderFor(field: FieldKey): string {
  switch (field) {
    case 'icao':
      return '3c4b17';
    case 'callsign':
      return 'LOT';
    case 'registration':
      return 'SP-LRF';
    case 'aircraft_type':
      return 'B738';
    case 'squawk':
      return '7700';
    default:
      return '';
  }
}

// Format the Date chip value. Renders both endpoints when present;
// shows an em-dash on the empty side for partial ranges.
function formatDateRange(from: string, to: string): string {
  const f = from ? shortDate(from) : '';
  const t = to ? shortDate(to) : '';
  if (f && t) return `${f}–${t}`;
  if (f) return `${f}–`;
  return `–${t}`;
}

function shortDate(yyyyMmDd: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(yyyyMmDd);
  if (!m) return yyyyMmDd;
  return `${Number(m[2])}/${Number(m[3])}`;
}

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
    </div>
  );
}
