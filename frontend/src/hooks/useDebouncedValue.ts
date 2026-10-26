import { useEffect, useState } from 'react';

// Trailing debounce: returns `value` only once it has been stable for `delayMs`.
// Use it to keep an input responsive while throttling the query it drives.
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(t);
  }, [value, delayMs]);
  return debounced;
}
