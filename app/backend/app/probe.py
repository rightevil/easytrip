"""A bounded real P0 run; HTTP success alone never means business success."""

import argparse
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from importlib.metadata import version

from .config import DATA_DIR, EXPORT_DIR, Settings, direct_client
from .integrations.xhs import (
    SourcePaused,
    connect,
    normalize_note,
    result_json,
    result_text,
)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def failure(error):
    # Exceptions may contain credential-bearing request URLs; export only their type.
    result = {"status": "不可用", "reason": type(error).__name__}
    if isinstance(error, BaseExceptionGroup):
        result["causes"] = [failure(item) for item in error.exceptions]
    if hasattr(error, "response"):
        result["http_status"] = error.response.status_code
    if isinstance(error, OSError):
        result["errno"] = error.errno
    if hasattr(error, "service_message"):
        result["service_message"] = error.service_message
    if hasattr(error, "infocode"):
        result["infocode"] = error.infocode
    return result


async def collect_xhs(settings, client, args, report):
    notes, seen, attempts = [], set(), 0
    started = time.perf_counter()
    save_json(DATA_DIR / "notes-checkpoint.json", [])
    report["xhs_sample"] = {
        "status": "未验证",
        "notes_budget": args.notes,
        "unique_notes_attempted": 0,
        "notes_read": 0,
        "comments_obtained": 0,
        "comments_selected": 0,
        "comments_analysed": 0,
        "replies_analysed": 0,
    }
    async with connect(settings.xhs_mcp, client) as mcp:
        report["xhs_mcp"] = {
            "status": "通过",
            "transport": "Streamable HTTP",
            "trust_env": False,
            "protocol": mcp.initialized.protocolVersion,
            "server": mcp.initialized.serverInfo.model_dump(),
            "tools": [
                {"name": tool.name, "inputSchema": tool.inputSchema}
                for tool in mcp.tools.values()
            ],
            "enabled_tools": sorted(
                name
                for name in mcp.tools
                if name in {"check_login_status", "search_feeds", "get_feed_detail"}
            ),
        }
        report["xhs_calls"] = mcp.calls
        print("小红书 MCP 初始化与只读工具发现完成", flush=True)
        login = result_text(await mcp.call("check_login_status", {}))
        if "已登录" not in login:
            raise SourcePaused("登录未确认，不扩大采集")
        report["xhs_login"] = {"status": "通过"}
        queries = [("明确目的地", args.query), ("主题探索", args.explore)]
        search_batches = []
        for kind, keyword in queries:
            query_start = time.perf_counter()
            data = result_json(await mcp.call("search_feeds", {"keyword": keyword}))
            feeds = data.get("feeds", [])
            search_batches.append((kind, feeds))
            report.setdefault("searches", []).append(
                {
                    "kind": kind,
                    "keyword": keyword,
                    "returned": len(feeds),
                    "fields": sorted(data),
                    "elapsed_seconds": round(time.perf_counter() - query_start, 3),
                }
            )
            print(f"{kind}搜索返回 {len(feeds)} 项", flush=True)
        # Interleave the two searches so a full first batch does not hide exploration.
        queue = []
        for index in range(max((len(feeds) for _, feeds in search_batches), default=0)):
            queue.extend(
                (kind, feeds[index])
                for kind, feeds in search_batches
                if index < len(feeds)
            )
        duplicates, paused = 0, False
        for kind, feed in queue:
            if attempts >= args.notes:
                break
            note_id, token = feed.get("id"), feed.get("xsecToken")
            if not note_id or not token:
                continue
            if note_id in seen:
                duplicates += 1
                continue
            seen.add(note_id)
            attempts += 1
            settings.secrets.add(token)
            call_start = time.perf_counter()
            try:
                payload = result_json(
                    await mcp.call(
                        "get_feed_detail",
                        {
                            "feed_id": note_id,
                            "xsec_token": token,
                            "load_all_comments": True,
                            "limit": args.comments,
                            "click_more_replies": False,
                        },
                    )
                )
                note = normalize_note(
                    payload, f"note-{attempts}", settings, args.comments
                )
                note["query_kind"] = kind
                note["elapsed_seconds"] = round(time.perf_counter() - call_start, 3)
                notes.append(note)
                print(f"已读取 {len(notes)}/{args.notes} 篇真实详情", flush=True)
            except SourcePaused as error:
                report["xhs_failure"] = failure(error)
                paused = True
                break
            except Exception as error:
                report.setdefault("detail_errors", []).append(
                    {"source_id": f"note-{attempts}", **failure(error)}
                )
            finally:
                # Safe checkpoint: credentials and author identities never land here.
                save_json(DATA_DIR / "notes-checkpoint.json", notes)
        report["xhs_calls"] = mcp.calls
        report["xhs_sample"] = {
            "status": "部分可用" if paused and notes else "通过" if notes else "不可用",
            "notes_budget": args.notes,
            "unique_notes_attempted": attempts,
            "notes_read": len(notes),
            "duplicates_skipped_before_stop": duplicates,
            "comments_budget_per_note": args.comments,
            "comments_obtained": sum(n["comments_obtained"] for n in notes),
            "comments_selected": sum(n["comments_selected"] for n in notes),
            "comments_analysed": 0,
            "replies_analysed": 0,
            "stop_reason": "来源暂停"
            if paused
            else "详情读取上限"
            if attempts >= args.notes
            else "可用搜索结果耗尽",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    return notes


def route_summary(mode, response):
    route = response.get("route", {})
    paths = route.get("transits" if mode == "transit" else "paths", [])
    if not paths:
        return {
            "mode": mode,
            "status": "部分可用",
            "reason": "真实接口返回空路线",
            "geometry": [],
        }
    path = paths[0]
    geometries = []

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "polyline" and isinstance(item, str):
                    points = [
                        [float(number) for number in point.split(",")]
                        for point in item.split(";")
                        if point
                    ]
                    if any(
                        len(point) != 2
                        or not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90)
                        for point in points
                    ):
                        raise ValueError("路线几何坐标不合法")
                    if len(points) > 1:
                        geometries.append(points)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(path)
    cost = path.get("cost", {})
    duration = cost.get("duration") if isinstance(cost, dict) else None
    if duration is None:
        duration = path.get("duration")

    def quantity(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 and number < float("inf") else None

    duration = quantity(duration)
    return {
        "mode": mode,
        "status": "通过" if geometries and duration is not None else "部分可用",
        "distance_meters": quantity(path.get("distance")),
        "duration_seconds": duration,
        "geometry": geometries,
        "actual_fields": sorted(path),
        "paths_returned": len(paths),
        "segments_returned": len(path.get("segments", []))
        if mode == "transit"
        else None,
    }


async def probe_amap(settings, client, args, notes, model_result, report):
    from .integrations.amap import Amap

    amap = Amap(client, settings.amap_web_key)
    places = ((model_result or {}).get("extraction") or {}).get("places", [])
    queries = (
        args.poi
        or list(dict.fromkeys(p["name"] for p in places if p.get("city") == args.city))[
            :3
        ]
    )
    if not queries:
        queries = ["浙江省博物馆孤山馆区", "楼外楼孤山路店", "杭州西湖国宾馆"]
        report["poi_selection"] = "人工构造的杭州接口测试地点，未声称由模型或样本产生"
    else:
        report["poi_selection"] = (
            "人工指定样本线索"
            if args.poi
            else "模型提取的同城名称；仅测试，尚未自动接受为真实行程地点"
        )
    candidates, responses = [], []
    for query in queries[:3]:
        started = time.perf_counter()
        try:
            response = await amap.search(query, args.city)
            pois = response.get("pois", [])
            item = {
                "query": query,
                "status": "通过" if pois else "部分可用",
                "count": len(pois),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "matches": pois[:3],
            }
            if pois:
                detail = await amap.detail(pois[0]["id"])
                actual = detail.get("pois", [])
                item["detail"] = actual[0] if actual else None
                if actual and actual[0].get("location"):
                    poi = actual[0]
                    candidates.append(
                        {
                            key: poi.get(key)
                            for key in (
                                "id",
                                "name",
                                "location",
                                "address",
                                "cityname",
                                "type",
                                "citycode",
                            )
                        }
                    )
                    item["verification"] = (
                        "真实返回地点和地址可复核；首结果只用于接口示例，不代表用户确认/自动匹配通过"
                    )
            responses.append(item)
        except Exception as error:
            responses.append({"query": query, **failure(error)})
    report["amap_pois"] = responses
    try:
        branches = await amap.search("星巴克", args.city)
        report["amap_ambiguity"] = {
            "status": "通过" if len(branches.get("pois", [])) > 1 else "部分可用",
            "query": "星巴克",
            "matches": branches.get("pois", [])[:3],
            "policy": "仅店名无法选择分店；保留各 POI ID/地址，不自动归并",
        }
    except Exception as error:
        report["amap_ambiguity"] = failure(error)
    routes = []
    if len(candidates) >= 2:
        origin, destination = candidates[0], candidates[1]
        for mode in ("walking", "transit", "bicycling", "electrobike"):
            started = time.perf_counter()
            try:
                response = await amap.route(
                    mode,
                    origin["location"],
                    destination["location"],
                    origin.get("citycode") or "0571",
                )
                summary = route_summary(mode, response)
                summary["elapsed_seconds"] = round(time.perf_counter() - started, 3)
                routes.append(summary)
            except Exception as error:
                routes.append({"mode": mode, **failure(error)})
        save_json(
            EXPORT_DIR / "map-data.json",
            {
                "origin": {
                    "name": f"测试起点：{origin['name']}（非用户确认）",
                    "location": origin["location"],
                },
                "candidates": candidates[1:],
                "routes": routes,
                "coordinate_system": "GCJ-02",
                "coordinate_order": "longitude,latitude",
                "test_city": args.city,
            },
        )
    report["amap_routes"] = routes
    report["amap_web_calls"] = len(amap.calls)
    report["amap_web_endpoints"] = amap.calls
    report["route_limits"] = (
        "公交测试按当前 API 可得数据；未校验用户出发日期、共享车辆、电量、还车区、铁路班次"
    )


async def run(args):
    settings = Settings.load()
    if not args.xhs_only:
        (EXPORT_DIR / "map-data.json").unlink(missing_ok=True)
    started = time.perf_counter()
    report = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "real_services": True,
        "versions": {name: version(name) for name in ("mcp", "httpx", "fastapi")},
        "direct_connection": {
            "application": "全部实际 HTTPX 客户端 trust_env=False；MCP transport 接收同一客户端",
            "xhs_service_browser": "未验证：本应用无法读取远端服务/浏览器出口配置",
        },
        "map_browser": {
            "status": "未验证",
            "reason": "需实际打开本机地图并核对 Marker/Polyline",
        },
        "manual_acceptance": "待用户复核：地点归属、正负评价、图中文字、分店及吃住缺口",
    }
    notes, model_result = [], None
    async with direct_client() as client:
        if args.reuse:
            notes = json.loads(
                (DATA_DIR / "notes-checkpoint.json").read_text(encoding="utf-8")
            )
            previous = DATA_DIR / "last-run.json"
            if previous.exists():
                old = json.loads(previous.read_text(encoding="utf-8"))
                report.update(
                    {
                        key: value
                        for key, value in old.items()
                        if key.startswith("xhs") or key == "searches"
                    }
                )
            report["sample_reused"] = "复用上次真实详情检查点；本轮未再次调用小红书"
        elif settings.xhs_mcp:
            try:
                async with asyncio.timeout(600):
                    notes = await collect_xhs(settings, client, args, report)
            except Exception as error:
                report["xhs_failure"] = failure(error)
                checkpoint = DATA_DIR / "notes-checkpoint.json"
                if checkpoint.exists():
                    notes = json.loads(checkpoint.read_text(encoding="utf-8"))
                print("小红书来源已停止：", type(error).__name__, flush=True)
        else:
            report["xhs_mcp"] = {"status": "未验证", "reason": "缺少 XHS_MCP"}
        print(
            f"真实笔记 {len(notes)} 篇，选取一级评论 {sum(len(n['comments']) for n in notes)} 条",
            flush=True,
        )
        if (
            notes
            and settings.model_base_url
            and settings.model_api_key
            and settings.model_id
        ):
            from .integrations.model import extract

            try:
                stage_start = time.perf_counter()
                model_result = await extract(client, settings, notes)
                model_result["elapsed_seconds"] = round(
                    time.perf_counter() - stage_start, 3
                )
                report["model"] = model_result
                if model_result.get("status") == "passed" and "xhs_sample" in report:
                    report["xhs_sample"]["comments_submitted"] = sum(
                        len(n["comments"]) for n in notes
                    )
                    report["xhs_sample"]["comments_analysed"] = len(
                        {
                            comment["source_id"]
                            for comment in (model_result.get("extraction") or {}).get(
                                "comments", []
                            )
                        }
                    )
                    report["xhs_sample"]["comments_unclassified"] = len(
                        model_result.get("unclassified_comment_ids", [])
                    )
                report["model_semantics"] = {
                    "status": "未验证",
                    "reason": "结构与引用校验由程序执行；评论对象、立场及图中文字仍需人工验收，不宣称准确率",
                }
                print("模型抽取：", model_result.get("status"), flush=True)
            except Exception as error:
                report["model"] = failure(error)
                print("模型抽取停止：", type(error).__name__, flush=True)
        else:
            report["model"] = {"status": "未验证", "reason": "缺少真实笔记或模型配置"}
        if not args.xhs_only and settings.amap_web_key:
            try:
                await probe_amap(settings, client, args, notes, model_result, report)
            except Exception as error:
                report["amap_web"] = failure(error)
            url = (
                settings.amap_mcp_url
                or f"https://mcp.amap.com/mcp?key={settings.amap_web_key}"
            )
            try:
                stage_start = time.perf_counter()
                async with connect(url, client) as mcp:
                    report["amap_mcp"] = {
                        "status": "通过",
                        "protocol": mcp.initialized.protocolVersion,
                        "server": mcp.initialized.serverInfo.model_dump(),
                        "tools": [
                            {"name": t.name, "inputSchema": t.inputSchema}
                            for t in mcp.tools.values()
                        ],
                        "elapsed_seconds": round(time.perf_counter() - stage_start, 3),
                        "business_calls": 0,
                        "note": "只核对工具发现；地点/线路由 Web API 验证，不重复同一查询",
                    }
            except Exception as error:
                report["amap_mcp"] = failure(error)
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    report["cost"] = (
        "记录实际 token/图片与 API 调用量；未核实 Agnes 定价/高德账号权益，不计算人民币费用"
    )
    exported_notes = [
        {key: value for key, value in note.items() if key != "images"} for note in notes
    ]
    save_json(EXPORT_DIR / "samples.json", settings.redact(exported_notes))
    save_json(EXPORT_DIR / "capabilities.json", settings.redact(report))
    save_json(DATA_DIR / "last-run.json", settings.redact(report))
    write_report(settings.redact(report), exported_notes)
    print(f"P0 实测产物已保存至 {EXPORT_DIR}", flush=True)


def write_report(report, notes):
    lines = [
        "# P0 真实服务能力报告",
        "",
        f"运行时间（北京时间）：{datetime.fromisoformat(report['run_at_utc']).astimezone(timezone(timedelta(hours=8))).isoformat()}。本轮链路耗时 {report['elapsed_seconds']} 秒。",
        "",
        "本报告只覆盖有限 P0 样本，不等同于完整行程产品或语义质量验收。",
        "",
        "| 能力 | 实测状态 | 依据 |",
        "|---|---|---|",
    ]
    for key in (
        "xhs_mcp",
        "xhs_login",
        "xhs_sample",
        "model",
        "model_semantics",
        "amap_mcp",
        "amap_ambiguity",
        "map_browser",
    ):
        item = report.get(key, {"status": "未验证"})
        status = {"passed": "通过", "failed": "不可用"}.get(
            item.get("status"), item.get("status", "未验证")
        )
        lines.append(f"| {key} | {status} | 见 capabilities.json 的 {key} 字段 |")
    for item in report.get("amap_pois", []):
        lines.append(
            f"| POI：{item['query']} | {item['status']} | 真实搜索及详情，分店需人工确认 |"
        )
    for route in report.get("amap_routes", []):
        lines.append(
            f"| 路线：{route['mode']} | {route['status']} | {route.get('duration_seconds', '未知')} 秒，{route.get('distance_meters', '未知')} 米；{route.get('reason', '')} |"
        )
    lines += [
        "",
        f"样本：{len(notes)} 篇唯一笔记；选取 {sum(len(note['comments']) for note in notes)} 条一级评论；二级回复关闭。",
        f"显式模型判断：{report.get('xhs_sample', {}).get('comments_analysed', 0)} 条唯一一级评论；未判断 {report.get('xhs_sample', {}).get('comments_unclassified', '见模型状态')} 条。",
        f"模型用量：{report.get('model', {}).get('usage', {})}；实际提交 {report.get('model', {}).get('images_submitted', 0)} 张图片，样本共有 {sum(note.get('image_count', 0) for note in notes)} 张，并未全部理解。",
        f"MCP发现：小红书 {len(report.get('xhs_mcp', {}).get('tools', []))} 工具，高德 {len(report.get('amap_mcp', {}).get('tools', []))} 工具；小红书工具调用 {len(report.get('xhs_calls', []))} 次，本轮高德Web调用 {report.get('amap_web_calls', 0)} 次。",
        "",
        "- 原始 note_id、用户身份、xsec_token、带签名链接不进入导出样本；本地检查点位于已忽略的 data/。",
        "- 所有正式坐标来自高德，GCJ-02，经度在前；测试起点未经用户确认为真实出发点。",
        "- POI来源选择方式见 capabilities.json 的 poi_selection；首结果仅为接口测试，正式POI消歧在P3实现。",
        f"- {report['direct_connection']['application']}。",
        f"- 小红书服务/浏览器出口：{report['direct_connection']['xhs_service_browser']}。",
        f"- 人工验收：{report['manual_acceptance']}。",
        f"- 成本：{report['cost']}。",
        "",
        "## 受阻能力和字段差异",
        "",
        "详见 field-differences.md 与 capabilities.json。缺字段保持未知，缺类别不追加无上限搜索。",
        "",
        "## 复现",
        "",
        "在 app/ 执行 `uv sync --locked`、`uv run python -m backend.app.probe`；`--reuse` 复用上轮已读详情，避免再次采集。",
        "",
        "地图：`uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --no-access-log`，打开 http://127.0.0.1:8000。",
    ]
    if "xhs_failure" in report:
        lines += [
            "",
            f"小红书停止原因：{report['xhs_failure']['reason']}；已保留完成的脱敏检查点。",
        ]
    (EXPORT_DIR / "capability-report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(description="P0 bounded real read-only probe")
    parser.add_argument("--query", default="杭州一日游 攻略")
    parser.add_argument("--explore", default="周末 安静 山水 高铁 旅行")
    parser.add_argument("--city", default="杭州")
    parser.add_argument(
        "--poi", action="append", help="人工从样本选出的同城 POI 名，最多3个"
    )
    parser.add_argument("--notes", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--comments", type=int, choices=range(1, 11), default=10)
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--xhs-only", action="store_true")
    for name in ("httpx", "httpcore", "mcp"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as error:
        print("探测未完成：", type(error).__name__, "；未输出可能含凭证的异常详情")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
