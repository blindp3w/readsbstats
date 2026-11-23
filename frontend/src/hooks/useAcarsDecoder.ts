import { useCallback, useEffect, useSyncExternalStore } from 'react';
import type { MessageDecoder } from '@airframes/acars-decoder';
import type { Vdl2Message } from '@/lib/types';
import { decodeAcars, type DecodedAcars } from '@/lib/acarsDecode';
import { decodeLocal } from '@/lib/acarsLocal';

// Shared airframes decoder store.
//
// `new MessageDecoder()` builds ~77 plugins, so it is created ONCE per page
// (module-level promise) and shared by every hook instance (the feed, the
// flight ACARS panel, ...). The dynamic import() is what keeps the
// ~53 KB-gz `vdl2-decoder` chunk out of the shell. A failed import/construct is
// retried once after RETRY_DELAY_MS; after the second failure the status is
// 'unavailable' for the rest of the page's life (local decoders keep working).
//
// Decode results are cached per message id (the same row is re-rendered on every
// scroll frame of the virtualized list). The cache is dropped whenever the
// decoder instance changes, so a result computed before the chunk loaded
// (local-only) never shadows the airframes result.

export type DecoderStatus = 'loading' | 'ready' | 'unavailable';

interface StoreState {
  decoder: MessageDecoder | null;
  status: DecoderStatus;
}

const RETRY_DELAY_MS = 2000;
const CACHE_MAX = 5000;

let state: StoreState = { decoder: null, status: 'loading' };
let decoderPromise: Promise<MessageDecoder> | null = null;
let failures = 0;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let generation = 0; // invalidates in-flight loads after __resetForTests()
const cache = new Map<number, DecodedAcars | null>();
const listeners = new Set<() => void>();

function setState(next: StoreState) {
  if (next.decoder !== state.decoder) cache.clear();
  state = next;
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

const getState = () => state;

function load() {
  if (decoderPromise || retryTimer || state.status !== 'loading') return;
  const gen = generation;
  decoderPromise = import('@airframes/acars-decoder').then(
    ({ MessageDecoder }) => new MessageDecoder(),
  );
  decoderPromise.then(
    (decoder) => {
      if (gen === generation) setState({ decoder, status: 'ready' });
    },
    () => {
      if (gen !== generation) return;
      decoderPromise = null;
      failures += 1;
      if (failures >= 2) {
        setState({ decoder: null, status: 'unavailable' });
      } else {
        retryTimer = setTimeout(() => {
          retryTimer = null;
          load();
        }, RETRY_DELAY_MS);
      }
    },
  );
}

// Test-only: forget all module state (decoder, failures, timers, cache).
export function __resetForTests() {
  generation += 1;
  if (retryTimer) clearTimeout(retryTimer);
  retryTimer = null;
  decoderPromise = null;
  failures = 0;
  cache.clear();
  state = { decoder: null, status: 'loading' };
}

// Status of the shared decoder, for UI hints. Does NOT trigger the load (only
// useAcarsDecoder does), so a page that merely shows the hint never pulls the chunk.
export function useAcarsDecoderStatus(): DecoderStatus {
  return useSyncExternalStore(subscribe, getState).status;
}

// Returns a decode fn that always works: airframes first (once its chunk has
// loaded), then the local decoders in lib/acarsLocal for formats airframes
// leaves undecoded. The local ones are tiny and need no import, so they decode
// immediately — before the airframes chunk resolves. The returned function is
// stable until the decoder instance changes, and memoizes by message id.
export function useAcarsDecoder(): (msg: Vdl2Message) => DecodedAcars | null {
  const { decoder } = useSyncExternalStore(subscribe, getState);

  useEffect(() => {
    load();
  }, []);

  return useCallback(
    (msg: Vdl2Message) => {
      const id = typeof msg.id === 'number' ? msg.id : null;
      if (id !== null && cache.has(id)) return cache.get(id) ?? null;
      const out = (decoder ? decodeAcars(msg, decoder) : null) ?? decodeLocal(msg);
      if (id !== null) {
        if (cache.size >= CACHE_MAX) cache.clear();
        cache.set(id, out);
      }
      return out;
    },
    [decoder],
  );
}
