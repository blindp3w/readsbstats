import { useEffect, useMemo, useState } from 'react';
import { useFormat } from '@/hooks/useFormat';
import { type Mode } from '@/components/map/MapModeControl';
import { type PlaybackSpeed } from '@/components/map/MapRewindControls';
import {
  clampHist,
  composeUnix,
  describeRewind,
  pad2,
  startOfDayLocal,
  unixToHHMM,
  unixToISO,
} from '@/lib/mapTime';

const PLAYBACK_TICK_MS = 1000; // 1 s real time per tick

// HIST default time-of-day when the user picks a date without touching the
// time picker. Noon catches the busy mid-day window for most receivers.
const HIST_DEFAULT_HOUR = 12;

export interface MapPlaybackState {
  // raw state
  mode: Mode;
  rewindOffsetSec: number;
  histAt: number | null;
  playing: boolean;
  speed: PlaybackSpeed;
  nowSec: number;
  // setters surfaced for the page's command-bar wiring
  setSpeed: (next: PlaybackSpeed) => void;
  setPlaying: (next: boolean | ((v: boolean) => boolean)) => void;
  // handlers
  handleModeChange: (next: Mode) => void;
  onScrubChange: (next: number) => void;
  onSeek: (deltaSec: number) => void;
  onJumpNow: () => void;
  onHistDateChange: (nextISO: string) => void;
  onHistTimeChange: (nextHHMM: string) => void;
  // derived
  scrubMin: number;
  scrubMax: number;
  scrubValue: number;
  rewindLabel: string;
  histDateISO: string;
  histTimeHHMM: string;
}

// Owns all map playback state + transitions, extracted verbatim from
// pages/Map.tsx. Takes `maxRewindSec` (from useMapSettings) so the page can wire
// it before useMapDataQueries (which needs the playback state for the snapshot
// query key) — breaking the settings↔playback↔data cycle.
export function useMapPlaybackState(maxRewindSec: number): MapPlaybackState {
  const { fmtTs } = useFormat();

  // ── core state ─────────────────────────────────────────────────────────
  const [mode, setMode] = useState<Mode>('live');
  const [rewindOffsetSec, setRewindOffsetSec] = useState(0);
  const [histAt, setHistAt] = useState<number | null>(null);

  // ── playback ──────────────────────────────────────────────────────────
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<PlaybackSpeed>(1);

  // `nowSec` (epoch seconds) drives the scrubber bounds. It must advance over
  // real time so the user can always scrub up to the present, but reading
  // `Date.now()` during render is impure (react-hooks/purity) and produced a
  // fresh value every render. Hold it in state and refresh on an interval —
  // 10 s matches the live snapshot poll; bounds a few seconds stale are
  // imperceptible against a multi-hour HIST window.
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => {
    const tick = () => setNowSec(Math.floor(Date.now() / 1000));
    tick();
    const id = window.setInterval(tick, 10_000);
    return () => window.clearInterval(id);
  }, []);

  // ── playback tick — advance scrub forward per real second ──────────────
  useEffect(() => {
    if (!playing) return;
    if (mode === 'live') return;
    const id = window.setInterval(() => {
      if (mode === 'rewind') {
        setRewindOffsetSec((v) => {
          const next = Math.max(0, v - speed * (PLAYBACK_TICK_MS / 1000));
          if (next === 0) {
            // Reached now — switch back to live and stop the timer.
            setPlaying(false);
            setMode('live');
          }
          return next;
        });
      } else {
        // HIST: advance histAt forward toward now. Audit 2026-06-01 S:
        // on catch-up, mirror the rewind branch and flip back to 'live'
        // (otherwise the UI sat in HIST mode with playback stopped).
        // Clamp the non-terminal advance against the same fresh clock used
        // for the catch-up check (matches clampHist's [now-MAX, now] bounds)
        // to avoid one-tick overshoot.
        setHistAt((v) => {
          if (v == null) return v;
          const nowSec = Math.floor(Date.now() / 1000);
          const next = v + speed * (PLAYBACK_TICK_MS / 1000);
          if (next >= nowSec) {
            setPlaying(false);
            setMode('live');
            return nowSec;
          }
          return Math.max(nowSec - maxRewindSec, Math.min(nowSec, next));
        });
      }
    }, PLAYBACK_TICK_MS);
    return () => window.clearInterval(id);
  }, [playing, mode, speed, maxRewindSec]);

  // ── mode-transition side effects ──────────────────────────────────────
  const handleModeChange = (next: Mode) => {
    if (next === mode) return;
    const nowSec = Math.floor(Date.now() / 1000);

    if (next === 'live') {
      setRewindOffsetSec(0);
      setHistAt(null);
      setPlaying(false);
      setMode('live');
      return;
    }

    if (next === 'rewind') {
      // If coming from HIST with a chosen time, preserve it as an offset.
      if (mode === 'hist' && histAt != null) {
        setRewindOffsetSec(Math.max(0, nowSec - histAt));
        setHistAt(null);
      }
      setPlaying(false);
      setMode('rewind');
      return;
    }

    // next === 'hist'
    if (mode === 'rewind' && rewindOffsetSec > 0) {
      setHistAt(nowSec - rewindOffsetSec);
      setRewindOffsetSec(0);
    } else {
      // Default to 1h ago so the map renders immediately on entry.
      setHistAt(nowSec - 3600);
    }
    setPlaying(false);
    setMode('hist');
  };

  // ── HIST date/time derivation ─────────────────────────────────────────
  const histDateISO = histAt != null ? unixToISO(histAt) : '';
  const histTimeHHMM = histAt != null ? unixToHHMM(histAt) : '';

  const onHistDateChange = (nextISO: string) => {
    const time = histAt != null ? unixToHHMM(histAt) : `${pad2(HIST_DEFAULT_HOUR)}:00`;
    const composed = composeUnix(nextISO, time);
    if (composed != null) setHistAt(clampHist(composed, nowSec, maxRewindSec));
  };
  const onHistTimeChange = (nextHHMM: string) => {
    const date = histAt != null ? unixToISO(histAt) : unixToISO(nowSec);
    const composed = composeUnix(date, nextHHMM);
    if (composed != null) setHistAt(clampHist(composed, nowSec, maxRewindSec));
  };

  // ── Scrubber bounds + label per mode ──────────────────────────────────
  const { scrubMin, scrubMax, scrubValue, rewindLabel } = useMemo(() => {
    if (mode === 'hist' && histAt != null) {
      const dayStart = startOfDayLocal(histAt);
      const dayEnd = dayStart + 86400 - 1;
      const min = Math.max(dayStart, nowSec - maxRewindSec);
      const max = Math.min(dayEnd, nowSec);
      return {
        scrubMin: min,
        scrubMax: max,
        scrubValue: Math.max(min, Math.min(max, histAt)),
        rewindLabel: fmtTs(histAt),
      };
    }
    // rewind (or hist with no date — won't show Row 2 but compute anyway)
    return {
      scrubMin: 0,
      scrubMax: maxRewindSec,
      scrubValue: rewindOffsetSec,
      rewindLabel: describeRewind(rewindOffsetSec),
    };
  }, [mode, histAt, rewindOffsetSec, maxRewindSec, nowSec, fmtTs]);

  // ── Bar callbacks ─────────────────────────────────────────────────────
  const onScrubChange = (next: number) => {
    if (mode === 'rewind') {
      setRewindOffsetSec(next);
    } else if (mode === 'hist') {
      setHistAt(clampHist(next, nowSec, maxRewindSec));
    }
    setPlaying(false);
  };
  const onSeek = (deltaSec: number) => {
    if (mode === 'rewind') {
      // delta>0 = go back; delta<0 = advance
      setRewindOffsetSec((v) => Math.max(0, Math.min(maxRewindSec, v + deltaSec)));
    } else if (mode === 'hist') {
      setHistAt((v) => (v == null ? v : clampHist(v - deltaSec, nowSec, maxRewindSec)));
    }
  };
  const onJumpNow = () => {
    setRewindOffsetSec(0);
    setHistAt(null);
    setMode('live');
    setPlaying(false);
  };

  return {
    mode,
    rewindOffsetSec,
    histAt,
    playing,
    speed,
    nowSec,
    setSpeed,
    setPlaying,
    handleModeChange,
    onScrubChange,
    onSeek,
    onJumpNow,
    onHistDateChange,
    onHistTimeChange,
    scrubMin,
    scrubMax,
    scrubValue,
    rewindLabel,
    histDateISO,
    histTimeHHMM,
  };
}
