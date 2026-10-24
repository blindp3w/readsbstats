import { describe, it, expect, vi } from 'vitest';
import { renderHook } from '@testing-library/react';

// setup.ts globally mocks the hook to null; this suite needs the real one.
vi.unmock('@/hooks/useAcarsDecoder');

import { useAcarsDecoder } from '@/hooks/useAcarsDecoder';
import type { Vdl2Message } from '@/lib/types';

describe('useAcarsDecoder local fallback', () => {
  it('decodes local formats immediately, before the airframes chunk resolves', () => {
    const { result } = renderHook(() => useAcarsDecoder());
    expect(result.current).not.toBeNull();
    const m = { id: 1, ts: 1, label: 'SA', body: '0EV123456V' } as Vdl2Message;
    expect(result.current!(m)?.description).toBe('Media advisory');
  });

  it('returns null for an undecodable message', () => {
    const { result } = renderHook(() => useAcarsDecoder());
    const m = { id: 2, ts: 1, label: 'H1', body: '#DFBABS' } as Vdl2Message;
    expect(result.current!(m)).toBeNull();
  });
});
