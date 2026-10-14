export type Coordinate = [number, number];
interface LngLat { lng: number; lat: number }
interface MapEvent { lnglat?: LngLat }
export interface AMapMarker {
  on(event: 'click', handler: () => void): void;
  setPosition(position: Coordinate): void;
}
export interface AMapPolyline { setPath(path: Coordinate[]): void }
export type AMapOverlay = AMapMarker | AMapPolyline;
export interface AMapInstance {
  on(event: string, handler: (event: MapEvent) => void): void;
  add(overlay: AMapOverlay): void;
  remove(overlay: AMapOverlay): void;
  getCenter(): LngLat;
  getZoom(): number;
  setZoomAndCenter(zoom: number, center: Coordinate): void;
  setFitView(): void;
  destroy(): void;
}
export interface AMapSDK {
  Map: new (element: HTMLElement, options: { zoom: number; center?: Coordinate; resizeEnable: boolean }) => AMapInstance;
  Marker: new (options: { position: Coordinate; content: HTMLElement; title: string; anchor: string }) => AMapMarker;
  Polyline: new (options: { path: Coordinate[]; strokeColor: string; strokeWeight: number; strokeOpacity: number; lineJoin: string; lineCap: string }) => AMapPolyline;
}
declare global {
  interface Window {
    AMap?: AMapSDK;
    _AMapSecurityConfig?: { serviceHost: string };
  }
}

export const validCoordinate = (value: unknown): value is Coordinate => Array.isArray(value) && value.length === 2
  && value.every(part => typeof part === 'number' && Number.isFinite(part)) && Math.abs(value[0]) <= 180 && Math.abs(value[1]) <= 90;

let loading: Promise<AMapSDK> | null = null;
export function loadAMap(): Promise<AMapSDK> {
  if (window.AMap) return Promise.resolve(window.AMap);
  if (loading) return loading;
  loading = (async () => {
    const response = await fetch('/api/map-config', { credentials: 'same-origin' });
    const config = await response.json().catch(() => null);
    if (!response.ok || typeof config?.key !== 'string' || !config.key) throw new Error('高德地图配置未就绪，请检查本机 JS Key 与安全密钥。');
    window._AMapSecurityConfig = { serviceHost: `${window.location.origin}/_AMapService` };
    await new Promise<void>((resolve, reject) => {
      const script = document.createElement('script');
      const fail = (reason: string) => { clearTimeout(timeout); script.remove(); reject(new Error(reason)); };
      const timeout = setTimeout(() => fail('高德 JS SDK 加载超时，请检查网络与 JS Key。'), 20000);
      script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(config.key)}`;
      script.onload = () => { clearTimeout(timeout); resolve(); };
      script.onerror = () => fail('高德 JS SDK 加载失败，请检查网络、JS Key 与域名设置。');
      document.head.append(script);
    });
    if (!window.AMap) throw new Error('高德 JS SDK 未就绪，请检查 JS Key 与域名设置。');
    return window.AMap;
  })().catch(error => { loading = null; throw error; });
  return loading;
}
