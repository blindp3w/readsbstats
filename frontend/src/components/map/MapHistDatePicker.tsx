import { DatePicker } from '@/components/ui/DatePicker';
import { TimePicker } from '@/components/ui/TimePicker';

interface Props {
  // Current picked moment as YYYY-MM-DD / HH:MM strings.
  dateISO: string;
  timeHHMM: string;
  onDateChange: (next: string) => void;
  onTimeChange: (next: string) => void;
  // Inclusive range of selectable dates, in unix seconds.
  minSec: number;
  maxSec: number;
  // Auto-open the date picker on first mount (used when transitioning to HIST).
  defaultOpen?: boolean;
}

// Date + time chip used in HIST mode. Composes the existing DatePicker and
// TimePicker primitives with side="top" so the popovers open above the
// bottom-fixed map command bar instead of clipping off-screen.
//
// The parent owns the conversion between (dateISO, timeHHMM) and the unix
// `histAt` value — this component is purely a UI shell.
export function MapHistDatePicker({
  dateISO,
  timeHHMM,
  onDateChange,
  onTimeChange,
  minSec,
  maxSec,
  defaultOpen,
}: Props) {
  const minDate = new Date(minSec * 1000);
  const maxDate = new Date(maxSec * 1000);

  return (
    <div className="flex items-center gap-1" data-testid="map-hist-date-picker">
      <div className="w-[160px]">
        <DatePicker
          value={dateISO}
          onChange={onDateChange}
          disabledMatcher={[{ before: minDate }, { after: maxDate }]}
          defaultOpen={defaultOpen}
          popoverSide="top"
          ariaLabel="History date"
          data-testid="map-hist-date"
        />
      </div>
      <div className="w-[100px]">
        <TimePicker
          value={timeHHMM}
          onChange={onTimeChange}
          popoverSide="top"
          ariaLabel="History time"
          data-testid="map-hist-time"
          minuteStep={5}
        />
      </div>
    </div>
  );
}
