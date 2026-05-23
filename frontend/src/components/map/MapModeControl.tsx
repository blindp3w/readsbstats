import { ToggleGroupRoot, ToggleGroupItem } from '@/components/ui/ToggleGroup';

export type Mode = 'live' | 'rewind' | 'hist';

interface Props {
  mode: Mode;
  onChange: (next: Mode) => void;
}

// Three-position segmented control. Transition side effects (resetting offsets,
// pausing playback, defaulting histAt) are the parent's concern — this
// component only emits the new mode.
export function MapModeControl({ mode, onChange }: Props) {
  return (
    <ToggleGroupRoot
      type="single"
      value={mode}
      onValueChange={(v) => {
        if (!v) return;
        onChange(v as Mode);
      }}
      aria-label="Map mode"
      data-testid="map-mode-group"
    >
      <ToggleGroupItem value="live" data-testid="map-mode-live">
        Live
      </ToggleGroupItem>
      <ToggleGroupItem value="rewind" data-testid="map-mode-rewind">
        Rewind
      </ToggleGroupItem>
      <ToggleGroupItem value="hist" data-testid="map-mode-hist">
        HIST
      </ToggleGroupItem>
    </ToggleGroupRoot>
  );
}
