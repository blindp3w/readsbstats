import { useEffect, useState } from 'react';
import type { MessageDecoder } from '@airframes/acars-decoder';
import type { Vdl2Message } from '@/lib/types';
import { decodeAcars, type DecodedAcars } from '@/lib/acarsDecode';

// Returns a decode fn once the decoder chunk has loaded, else null (callers show
// raw until then). The decoder lives in state (not a ref) so reading it during
// render is legitimate; the dynamic import is what keeps it in its own lazy
// chunk. The mounted guard avoids a state update after unmount when the import
// resolves late (keeps the VDL2 test suites act()-clean).
export function useAcarsDecoder(): ((msg: Vdl2Message) => DecodedAcars | null) | null {
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

  return decoder ? (msg: Vdl2Message) => decodeAcars(msg, decoder) : null;
}
