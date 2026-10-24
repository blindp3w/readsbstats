import { useEffect, useState } from 'react';
import type { MessageDecoder } from '@airframes/acars-decoder';
import type { Vdl2Message } from '@/lib/types';
import { decodeAcars, type DecodedAcars } from '@/lib/acarsDecode';
import { decodeLocal } from '@/lib/acarsLocal';

// Returns a decode fn that always works: airframes first (once its chunk has
// loaded), then the local decoders in lib/acarsLocal for formats airframes
// leaves undecoded. The local ones are tiny and need no import, so they decode
// immediately — before the airframes chunk resolves. The decoder lives in state
// (not a ref) so reading it during render is legitimate; the dynamic import is
// what keeps it in its own lazy chunk. The mounted guard avoids a state update
// after unmount when the import resolves late (keeps the VDL2 test suites
// act()-clean).
export function useAcarsDecoder(): (msg: Vdl2Message) => DecodedAcars | null {
  const [decoder, setDecoder] = useState<MessageDecoder | null>(null);

  useEffect(() => {
    let mounted = true;
    import('@airframes/acars-decoder')
      .then(({ MessageDecoder }) => {
        if (mounted) setDecoder(new MessageDecoder());
      })
      .catch(() => {
        /* decoder unavailable — stay null, raw body is shown */
      });
    return () => {
      mounted = false;
    };
  }, []);

  return (msg: Vdl2Message) => (decoder ? decodeAcars(msg, decoder) : null) ?? decodeLocal(msg);
}
