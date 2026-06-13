import { useEffect, useRef, useState } from 'react';
import type { Vdl2Message } from '@/lib/types';
import { decodeAcars, type DecodedAcars } from '@/lib/acarsDecode';

// Returns a decode fn once the decoder chunk has loaded, else null (callers show
// raw until then). The mounted guard prevents a state update after unmount when
// the dynamic import resolves late (keeps the VDL2 test suites act()-clean).
export function useAcarsDecoder(): ((msg: Vdl2Message) => DecodedAcars | null) | null {
  const decoderRef = useRef<{ decode: (m: { label: string; text: string }) => unknown } | null>(
    null,
  );
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let mounted = true;
    import('@airframes/acars-decoder')
      .then(({ MessageDecoder }) => {
        if (!mounted) return;
        decoderRef.current = new MessageDecoder();
        setReady(true);
      })
      .catch(() => {
        /* decoder unavailable — stay null, raw body is shown */
      });
    return () => {
      mounted = false;
    };
  }, []);

  if (!ready || !decoderRef.current) return null;
  const decoder = decoderRef.current;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (msg: Vdl2Message) => decodeAcars(msg, decoder as any);
}
