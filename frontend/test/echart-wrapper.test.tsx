import { vi, describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/react';

// Undo the global EChart stub so we test the real wrapper. We must also
// mock echarts/core itself because jsdom doesn't have canvas.
vi.unmock('@/components/charts/EChart');

// vi.mock hoists to top of file; pull spy creation into vi.hoisted so the
// factory below can close over the same references the test reads.
const { init, connect, disconnect, fakeInstance, setOption, resize, dispose, on, off } = vi.hoisted(
  () => {
    const setOption = vi.fn();
    const resize = vi.fn();
    const dispose = vi.fn();
    const on = vi.fn();
    const off = vi.fn();
    const fakeInstance: any = { setOption, resize, dispose, on, off, group: '' };
    const init = vi.fn(() => fakeInstance);
    const connect = vi.fn();
    const disconnect = vi.fn();
    return { init, connect, disconnect, fakeInstance, setOption, resize, dispose, on, off };
  },
);

vi.mock('@/components/charts/echarts-setup', () => ({
  echarts: { init, connect, disconnect, use: vi.fn() },
}));

// jsdom doesn't implement ResizeObserver — provide a no-op.
class FakeRO {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as any).ResizeObserver = (globalThis as any).ResizeObserver ?? FakeRO;

import { EChart } from '@/components/charts/EChart';

describe('<EChart>', () => {
  beforeEach(() => {
    setOption.mockClear();
    resize.mockClear();
    dispose.mockClear();
    on.mockClear();
    off.mockClear();
    init.mockClear();
    connect.mockClear();
    disconnect.mockClear();
    fakeInstance.group = '';
  });

  it('mounts ECharts on the host div with canvas renderer', () => {
    render(<EChart option={{ series: [] }} />);
    expect(init).toHaveBeenCalledTimes(1);
    const args = (init.mock.calls[0] as unknown[]) ?? [];
    expect(args[2]).toEqual({ renderer: 'canvas' });
  });

  it('applies the option with notMerge + lazyUpdate', () => {
    const option = { series: [{ type: 'line' as const, data: [] }] };
    render(<EChart option={option} />);
    expect(setOption).toHaveBeenCalledWith(option, { notMerge: true, lazyUpdate: true });
  });

  it('wires group + calls echarts.connect when group prop is set', () => {
    render(<EChart option={{ series: [] }} group="metrics" />);
    expect(fakeInstance.group).toBe('metrics');
    expect(connect).toHaveBeenCalledWith('metrics');
  });

  it('does not call connect when group prop is omitted', () => {
    render(<EChart option={{ series: [] }} />);
    expect(connect).not.toHaveBeenCalled();
  });

  // BUG-18 (code-review fix): on unmount the group effect must detach THIS chart
  // without tearing down the whole shared group. echarts.disconnect(group) is
  // group-wide — calling it here un-syncs every sibling chart still mounted in the
  // same group (the Metrics page mounts several group="metrics" charts at once).
  // Single-instance removal is just clearing chart.group; dispose() also drops the
  // instance from echarts' connected-group registry.
  it('clears chart.group on unmount without disconnecting the whole group', () => {
    const { unmount } = render(<EChart option={{ series: [] }} group="metrics" />);
    expect(fakeInstance.group).toBe('metrics');
    unmount();
    expect(disconnect).not.toHaveBeenCalled();
    expect(fakeInstance.group).not.toBe('metrics');
  });

  it('disposes on unmount', () => {
    const { unmount } = render(<EChart option={{ series: [] }} />);
    expect(dispose).not.toHaveBeenCalled();
    unmount();
    expect(dispose).toHaveBeenCalledTimes(1);
  });

  it('binds onEvents handlers and unbinds on cleanup', () => {
    const click = vi.fn();
    const { unmount } = render(<EChart option={{ series: [] }} onEvents={{ click }} />);
    expect(on).toHaveBeenCalledWith('click', click);
    unmount();
    expect(off).toHaveBeenCalledWith('click', click);
  });
});
