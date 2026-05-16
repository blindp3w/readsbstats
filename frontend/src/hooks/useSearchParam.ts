import { useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';

// Typed read+write of a single URL search param. Replaces the v1 vanilla JS
// pattern of "store filters in window.location.search so back-button works".
//
// Two overloads — number and string — keep usage type-safe without forcing
// callers to specify generics. Empty/default values are stripped from the URL
// so it doesn't fill up with `?callsign=&type=&page=0`.
//
// CAVEAT: don't call multiple useSearchParam setters back-to-back in the same
// event handler. React Router v7's setSearchParams reads stale state when
// invoked twice synchronously — the second call overwrites the first.
// Use `useSearchParamBatch()` below to apply multiple param changes atomically.

export type ParamValue = string | number;

export function useSearchParam(
  key: string,
  defaultValue: number,
): [number, (next: number) => void];
export function useSearchParam(
  key: string,
  defaultValue: string,
): [string, (next: string) => void];
export function useSearchParam(
  key: string,
  defaultValue: string | number,
): [string | number, (next: string | number) => void] {
  const [params, setParams] = useSearchParams();
  const raw = params.get(key);

  let value: string | number;
  if (raw == null) {
    value = defaultValue;
  } else if (typeof defaultValue === 'number') {
    const n = Number(raw);
    value = Number.isFinite(n) ? n : defaultValue;
  } else {
    value = raw;
  }

  const setValue = useCallback(
    (next: string | number) => {
      setParams(
        (prev) => {
          const out = new URLSearchParams(prev);
          const isDefault = isDefaultValue(next, defaultValue);
          if (isDefault) {
            out.delete(key);
          } else {
            out.set(key, String(next));
          }
          return out;
        },
        { replace: false },
      );
    },
    [key, defaultValue, setParams],
  );

  return [value, setValue];
}

function isDefaultValue(next: ParamValue, defaultValue: ParamValue): boolean {
  if (typeof defaultValue === 'number') return next === defaultValue;
  return next === defaultValue || next === '';
}

// Apply multiple URL param changes in a SINGLE setSearchParams call.
//
// Workaround for React Router v7's batching gap: calling setSearchParams twice
// back-to-back from the same event handler with the function form causes the
// second call to read stale `prev` (still the URL before the first call
// committed). Result: the second update silently overwrites the first.
//
// Use this whenever a UI event needs to touch more than one param at once
// (e.g. "clicking Military filter resets pagination to page 0"):
//
//   const update = useSearchParamBatch();
//   onClick={() => update({ flags: 'military', offset: 0 })};
//
// Values that match the empty-string / zero default are stripped from the URL.
// Pass `null` to explicitly remove a param.
export function useSearchParamBatch(): (updates: Record<string, ParamValue | null>) => void {
  const [, setParams] = useSearchParams();
  return useCallback(
    (updates) => {
      setParams(
        (prev) => {
          const out = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(updates)) {
            if (v === null || v === '' || v === 0) {
              out.delete(k);
            } else {
              out.set(k, String(v));
            }
          }
          return out;
        },
        { replace: false },
      );
    },
    [setParams],
  );
}
