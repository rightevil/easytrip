import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AMapCanvas } from './AMapCanvas';
import type { MapPoint } from './AMapCanvas';
import { loadAMap } from './amap';
import type { AMapSDK } from './amap';

vi.mock('./amap', async importOriginal => ({ ...await importOriginal<typeof import('./amap')>(), loadAMap: vi.fn() }));
class FakeMarker {
  on = vi.fn();
  setPosition = vi.fn();
  constructor(readonly options: { title: string; content: HTMLElement }) { created.push(this); }
}
class FakeMap {
  handlers = new Map<string, (event: { lnglat?: { lng: number; lat: number } }) => void>();
  on = (name: string, handler: (event: { lnglat?: { lng: number; lat: number } }) => void) => { this.handlers.set(name, handler); };
  add = vi.fn(); remove = vi.fn(); destroy = vi.fn(); setFitView = vi.fn(); setZoomAndCenter = vi.fn();
  getCenter = () => ({ lng: 120.1, lat: 30.2 });
  getZoom = () => 12;
  constructor() { instance = this; }
}
let instance: FakeMap;
let created: FakeMarker[];
const point = (id: string): MapPoint => ({ id, name: id, location: [120.1, 30.2] });
const defaults = () => ({ center: null, zoom: 12, focus: null, picking: false, onSelect: vi.fn(), onViewport: vi.fn(), onPick: vi.fn() });
beforeEach(() => { created = []; vi.mocked(loadAMap).mockResolvedValue({ Map: FakeMap, Marker: FakeMarker } as unknown as AMapSDK); });
afterEach(() => { cleanup(); vi.clearAllMocks(); });
async function ready() { await act(async () => { await Promise.resolve(); }); act(() => instance.handlers.get('complete')?.({})); }

describe('real SDK canvas', () => {
  it('adds a ninth point without recreating markers and deduplicates the same POI', async () => {
    const props = defaults();
    const points = [{ ...point('origin'), origin: true }, ...Array.from({ length: 8 }, (_, index) => point(`poi-${index}`))];
    const view = render(<AMapCanvas {...props} points={points} />); await ready();
    expect(created).toHaveLength(9);
    expect(created[0].options.content.textContent).toBe('起');
    expect(created[1].options.content.textContent).toBe('1');
    view.rerender(<AMapCanvas {...props} points={[...points, point('poi-8'), point('poi-8')]} />);
    expect(created).toHaveLength(10);
    expect(created[9].options.content.textContent).toBe('9');
    expect(instance.remove).not.toHaveBeenCalled();
    expect(instance.setFitView).toHaveBeenCalledTimes(1);
    view.rerender(<AMapCanvas {...props} points={[point('poi-8')]} focus={{ id: 'poi-8', sequence: 1 }} />);
    expect(instance.remove).toHaveBeenCalledTimes(9);
    expect(instance.setZoomAndCenter).toHaveBeenCalledWith(15, [120.1, 30.2]);
    expect(created[9].options.content.className).toContain('active-pin');
  });
  it('only accepts map clicks during explicit origin picking and persists valid viewport events', async () => {
    const props = defaults();
    const view = render(<AMapCanvas {...props} points={[]} />); await ready();
    act(() => instance.handlers.get('click')?.({ lnglat: { lng: 120.1, lat: 30.2 } }));
    expect(props.onPick).not.toHaveBeenCalled();
    view.rerender(<AMapCanvas {...props} picking points={[]} />);
    act(() => instance.handlers.get('click')?.({ lnglat: { lng: 120.1, lat: 30.2 } }));
    expect(props.onPick).toHaveBeenCalledWith([120.1, 30.2]);
    act(() => instance.handlers.get('click')?.({ lnglat: { lng: NaN, lat: 30.2 } }));
    expect(props.onPick).toHaveBeenCalledTimes(1);
    act(() => instance.handlers.get('moveend')?.({}));
    expect(props.onViewport).toHaveBeenCalledWith([120.1, 30.2], 12);
  });
  it('shows SDK failure explicitly without creating a map', async () => {
    vi.mocked(loadAMap).mockRejectedValue(new Error('地图配置未就绪'));
    render(<AMapCanvas {...defaults()} points={[point('poi')]} />);
    expect((await screen.findByRole('alert')).textContent).toContain('地图配置未就绪');
    expect(screen.getByText('地图配置未就绪')).toBeTruthy();
    expect(created).toHaveLength(0);
  });
});
