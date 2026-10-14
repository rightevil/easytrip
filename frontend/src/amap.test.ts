import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => { delete window.AMap; delete window._AMapSecurityConfig; document.querySelectorAll('script').forEach(script => script.remove()); vi.unstubAllGlobals(); vi.resetModules(); });
describe('SDK loading boundary', () => {
  it('rejects missing configuration without loading a script', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, json: async () => ({ detail: 'missing key' }) }));
    const { loadAMap } = await import('./amap');
    await expect(loadAMap()).rejects.toThrow('配置未就绪');
    expect(document.querySelector('script')).toBeNull();
  });
  it('uses only the public key and fixed same-origin security proxy and allows retry after SDK error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ key: 'public-js-key', serviceHost: 'https://untrusted.example/proxy' }) }));
    const { loadAMap } = await import('./amap');
    const first = loadAMap();
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    const script = document.querySelector('script')!;
    expect(script.src).toBe('https://webapi.amap.com/maps?v=2.0&key=public-js-key');
    expect(window._AMapSecurityConfig).toEqual({ serviceHost: `${window.location.origin}/_AMapService` });
    script.dispatchEvent(new Event('error'));
    await expect(first).rejects.toThrow('加载失败');
    const second = loadAMap();
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    document.querySelector('script')!.dispatchEvent(new Event('error'));
    await expect(second).rejects.toThrow('加载失败');
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
