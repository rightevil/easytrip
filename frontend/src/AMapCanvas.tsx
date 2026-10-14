import { useEffect, useRef, useState } from 'react';
import { loadAMap, validCoordinate } from './amap';
import type { AMapInstance, AMapMarker, AMapPolyline, AMapSDK, Coordinate } from './amap';

export interface MapPoint { id: string; name: string; location: Coordinate; origin?: boolean }
export interface MapRoute { id: string; mode: string; geometry: Coordinate[][] }
interface Props {
  points: MapPoint[];
  routes?: MapRoute[];
  center: Coordinate | null;
  zoom: number;
  focus: { id: string; sequence: number } | null;
  picking: boolean;
  pickNotice?: string;
  onViewport: (center: Coordinate, zoom: number) => void;
  onSelect: (id: string) => void;
  onPick: (location: Coordinate) => void;
}

export function AMapCanvas(props: Props) {
  const container = useRef<HTMLDivElement>(null);
  const current = useRef(props);
  current.current = props;
  const map = useRef<AMapInstance | null>(null);
  const sdk = useRef<AMapSDK | null>(null);
  const markers = useRef(new Map<string, { marker: AMapMarker; pin: HTMLElement }>());
  const polylines = useRef<AMapPolyline[]>([]);
  const fitted = useRef(false);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let alive = true;
    let complete = false;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    setReady(false); setError(null); fitted.current = false;
    void loadAMap().then(AMap => {
      if (!alive || !container.current) return;
      sdk.current = AMap;
      const instance = new AMap.Map(container.current, { zoom: current.current.center ? current.current.zoom : 4, center: current.current.center ?? [105, 35], resizeEnable: true });
      map.current = instance;
      timeout = setTimeout(() => { if (alive) setError('高德底图加载超时，请检查网络、JS Key 与域名设置。'); }, 20000);
      instance.on('complete', () => { if (alive) { complete = true; clearTimeout(timeout); setReady(true); setError(null); } });
      const viewport = () => {
        if (!alive || !complete || current.current.points.length > 0 && !fitted.current) return;
        const center = instance.getCenter();
        const coordinate: Coordinate = [center.lng, center.lat];
        const zoom = instance.getZoom();
        if (validCoordinate(coordinate) && Number.isFinite(zoom) && zoom >= 3 && zoom <= 20) current.current.onViewport(coordinate, zoom);
      };
      instance.on('moveend', viewport); instance.on('zoomend', viewport);
      instance.on('click', event => {
        if (alive && current.current.picking && event.lnglat) {
          const location: Coordinate = [event.lnglat.lng, event.lnglat.lat];
          if (validCoordinate(location)) current.current.onPick(location);
        }
      });
    }).catch(reason => { if (alive) setError(reason instanceof Error ? reason.message : '高德地图加载失败。'); });
    return () => { alive = false; clearTimeout(timeout); map.current?.destroy(); map.current = null; sdk.current = null; markers.current.clear(); polylines.current = []; };
  }, [attempt]);
  useEffect(() => {
    const instance = map.current, AMap = sdk.current;
    if (!ready || !instance || !AMap) return;
    const points = [...new Map(props.points.filter(point => validCoordinate(point.location)).map(point => [point.id, point])).values()];
    const ids = new Set(points.map(point => point.id));
    markers.current.forEach((item, id) => { if (!ids.has(id)) { instance.remove(item.marker); markers.current.delete(id); } });
    let candidateIndex = 0;
    points.forEach(point => {
      let item = markers.current.get(point.id);
      if (!item) {
        const pin = document.createElement('div');
        const marker = new AMap.Marker({ position: point.location, content: pin, title: point.name, anchor: 'center' });
        marker.on('click', () => current.current.onSelect(point.id));
        item = { marker, pin }; markers.current.set(point.id, item); instance.add(marker);
      } else item.marker.setPosition(point.location);
      item.pin.className = `map-pin${point.origin ? ' origin-pin' : ''}${props.focus?.id === point.id ? ' active-pin' : ''}`;
      item.pin.textContent = point.origin ? '起' : String(++candidateIndex);
      item.pin.title = point.name;
    });
    if (!fitted.current && points.length) {
      fitted.current = true;
      if (!props.center) instance.setFitView();
    }
  }, [ready, props.points, props.focus?.id, props.center]);
  useEffect(() => {
    const instance = map.current, AMap = sdk.current;
    if (!ready || !instance || !AMap) return;
    polylines.current.forEach(line => instance.remove(line));
    polylines.current = [];
    const colors: Record<string, string> = { walking: '#547642', transit: '#4269a4', bicycling: '#b07a25', electrobike: '#92743c', driving: '#8e537b' };
    (props.routes ?? []).forEach(route => route.geometry.forEach(path => {
      if (path.length < 2 || !path.every(validCoordinate)) return;
      const line = new AMap.Polyline({ path, strokeColor: colors[route.mode] ?? '#66745e', strokeWeight: 5, strokeOpacity: 0.85, lineJoin: 'round', lineCap: 'round' });
      polylines.current.push(line); instance.add(line);
    }));
    if (polylines.current.length && !props.center) instance.setFitView();
    return () => { polylines.current.forEach(line => instance.remove(line)); polylines.current = []; };
  }, [ready, props.routes]);
  useEffect(() => {
    const instance = map.current;
    if (!ready || !instance || !props.center) return;
    const center = instance.getCenter();
    if (Math.abs(center.lng - props.center[0]) > 0.000001 || Math.abs(center.lat - props.center[1]) > 0.000001 || instance.getZoom() !== props.zoom) instance.setZoomAndCenter(props.zoom, props.center);
  }, [ready, props.center, props.zoom]);
  useEffect(() => {
    if (!ready || !props.focus) return;
    const point = props.points.find(item => item.id === props.focus?.id);
    if (point) map.current?.setZoomAndCenter(Math.max(props.zoom, 15), point.location);
  }, [ready, props.focus?.sequence]);
  return <div className={`map-surface${props.picking ? ' picking-origin' : ''}`}>
    <div ref={container} className="amap-container" aria-label="高德地图：已确认的候选与出发点" />
    {!ready && !error && <p className="map-load-status" role="status">正在加载高德真实地图…</p>}
    {error && <div className="map-load-status map-load-error" role="alert"><p>{error}</p><button onClick={() => setAttempt(value => value + 1)}>重新加载地图</button></div>}
    {ready && props.picking && <p className="map-pick-notice" role="status">{props.pickNotice ?? '点击地图选择出发点，再确认保存。'}</p>}
  </div>;
}
