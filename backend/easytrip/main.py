"""Local P0 map viewer. Credentials and the JS security proxy stay on the backend."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import EXPORT_DIR, Settings, direct_client


def create_app(settings: Settings | None = None, export_dir: Path = EXPORT_DIR):
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(application):
        async with direct_client() as client:
            application.state.http = client
            yield

    application = FastAPI(title="EasyTrip P0 地图验证", lifespan=lifespan)
    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"]
    )

    @application.middleware("http")
    async def same_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return Response("不允许跨来源访问本机服务", status_code=403)
        return await call_next(request)

    @application.get("/")
    async def map_page():
        return FileResponse(Path(__file__).parent / "static" / "map.html")

    @application.get("/api/map-config")
    async def map_config(request: Request):
        if not settings.amap_js_key or not settings.amap_js_secure_key:
            raise HTTPException(503, "高德 JS Key 或安全密钥未配置")
        return {
            "key": settings.amap_js_key,
            "serviceHost": str(request.base_url).rstrip("/") + "/_AMapService",
        }

    @application.get("/api/map-data")
    async def map_data():
        try:
            payload = json.loads(
                (export_dir / "map-data.json").read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            raise HTTPException(404, "尚无 P0 地图样本，请先运行验证命令") from None
        except (OSError, ValueError):
            raise HTTPException(500, "P0 地图样本无法读取") from None
        if (
            not isinstance(payload, dict)
            or payload.get("coordinate_system") != "GCJ-02"
        ):
            raise HTTPException(500, "地图样本需声明 GCJ-02 坐标系")
        return settings.redact(
            {
                key: payload.get(key)
                for key in ("origin", "candidates", "routes", "coordinate_system")
            }
        )

    @application.get("/_AMapService/{path:path}")
    async def amap_proxy(path: str, request: Request):
        upstreams = {
            "v4/maps": "https://restapi.amap.com/v4/maps",
            "v3/log/init": "https://restapi.amap.com/v3/log/init",
            "v4/map/styles": "https://webapi.amap.com/v4/map/styles",
        }
        if path not in upstreams:
            raise HTTPException(404, "未开放此高德代理路径")
        if not settings.amap_js_key or not settings.amap_js_secure_key:
            raise HTTPException(503, "高德 JS Key 或安全密钥未配置")
        params = dict(request.query_params)
        params.update(key=settings.amap_js_key, jscode=settings.amap_js_secure_key)
        try:
            response = await application.state.http.get(upstreams[path], params=params)
        except httpx.HTTPError:
            raise HTTPException(502, "高德地图代理请求失败") from None
        if response.is_redirect:
            raise HTTPException(502, "高德地图代理不接受重定向")
        body = response.content
        for secret in (settings.amap_web_key, settings.amap_js_secure_key):
            if secret:
                body = body.replace(secret.encode(), b"[REDACTED]")
        return Response(
            body,
            status_code=response.status_code,
            headers={
                "Content-Type": response.headers.get(
                    "content-type", "application/octet-stream"
                ),
                "Cache-Control": "no-store",
            },
        )

    return application


app = create_app()
