import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.app import probe
from backend.app.config import Settings
from backend.app.integrations.xhs import SourcePaused
from backend.app.probe import collect_xhs, failure, route_summary, save_json


@pytest.mark.parametrize(
    "response",
    [
        {"route": {"paths": []}},
        {
            "route": {
                "paths": [
                    {
                        "duration": "600",
                        "distance": "1500",
                        "origin": "120,30",
                        "destination": "121,31",
                    }
                ]
            }
        },
        {"route": {"paths": [{"duration": "600", "steps": [{"polyline": "120,30"}]}]}},
    ],
)
def test_route_without_real_multi_point_polyline_has_no_geometry(response):
    summary = route_summary("walking", response)
    assert summary["status"] == "部分可用"
    assert summary["geometry"] == []


def test_route_uses_only_first_returned_path_and_preserves_polyline_segments():
    response = {
        "route": {
            "paths": [
                {
                    "distance": "1200",
                    "cost": {"duration": "600"},
                    "steps": [
                        {"polyline": "120.1,30.1;120.2,30.2"},
                        {"polyline": "120.3,30.3;120.4,30.4"},
                    ],
                },
                {"steps": [{"polyline": "121,31;122,32"}]},
            ]
        }
    }
    summary = route_summary("walking", response)
    assert summary["status"] == "通过"
    assert summary["geometry"] == [
        [[120.1, 30.1], [120.2, 30.2]],
        [[120.3, 30.3], [120.4, 30.4]],
    ]
    assert summary["distance_meters"] == 1200
    assert summary["duration_seconds"] == 600
    assert summary["paths_returned"] == 2


@pytest.mark.parametrize("cost", [{"duration": "420", "transit_fee": "2.00"}, "2.00"])
def test_transit_duration_and_distance_are_seconds_and_meters_not_fare(cost):
    summary = route_summary(
        "transit",
        {
            "route": {
                "transits": [
                    {
                        "distance": "2200",
                        "duration": "420",
                        "cost": cost,
                        "segments": [
                            {"walking": {"steps": [{"polyline": "120,30;120.1,30.1"}]}}
                        ],
                    }
                ]
            }
        },
    )
    assert summary["status"] == "通过"
    assert summary["duration_seconds"] == 420
    assert summary["distance_meters"] == 2200
    assert summary["geometry"] == [[[120.0, 30.0], [120.1, 30.1]]]


@pytest.mark.parametrize(
    "polyline",
    [
        "181,30;120,30",
        "120,91;120,30",
        "nan,30;120,30",
        "120,inf;120,30",
        "120,30,0;120,30",
        "bad,30;120,30",
    ],
)
def test_route_rejects_invalid_upstream_coordinates(polyline):
    with pytest.raises(ValueError):
        route_summary(
            "walking", {"route": {"paths": [{"steps": [{"polyline": polyline}]}]}}
        )


@pytest.mark.parametrize("duration", ["nan", -1, True, ""])
def test_invalid_duration_stays_unknown_with_partial_route_status(duration):
    summary = route_summary(
        "walking",
        {
            "route": {
                "paths": [
                    {
                        "duration": duration,
                        "distance": duration,
                        "steps": [{"polyline": "120,30;120.1,30.1"}],
                    }
                ]
            }
        },
    )
    assert summary["geometry"] == [[[120.0, 30.0], [120.1, 30.1]]]
    assert summary["duration_seconds"] is None
    assert summary["distance_meters"] is None
    assert summary["status"] == "部分可用"


def test_failure_export_never_includes_exception_message():
    assert failure(ValueError("https://service.example/?key=private 13800138000")) == {
        "status": "不可用",
        "reason": "ValueError",
    }


def test_save_json_replaces_the_checkpoint_and_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "data" / "checkpoint.json"
    save_json(target, {"notes": ["西湖"]})
    save_json(target, {"notes": ["孤山"]})
    assert json.loads(target.read_text(encoding="utf-8")) == {"notes": ["孤山"]}
    assert not target.with_suffix(".tmp").exists()


def test_collection_interleaves_searches_deduplicates_and_obeys_detail_budget(
    monkeypatch, tmp_path
):
    calls = []
    searches = {
        "明确查询": [
            {"id": "a", "xsecToken": "token-a"},
            {"id": "a", "xsecToken": "token-a"},
            {"id": "b", "xsecToken": "token-b"},
        ],
        "主题查询": [
            {"id": "a", "xsecToken": "token-a"},
            {"id": "c", "xsecToken": "token-c"},
        ],
    }

    async def call(name, arguments):
        calls.append((name, arguments))
        if name == "check_login_status":
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="已登录")]
            )
        if name == "search_feeds":
            payload = {"feeds": searches[arguments["keyword"]]}
        else:
            payload = {
                "data": {
                    "note": {"title": "西湖", "desc": "正文"},
                    "comments": {"list": []},
                }
            }
        return SimpleNamespace(structuredContent=payload)

    mcp = SimpleNamespace(
        initialized=SimpleNamespace(
            protocolVersion="2025-11-25",
            serverInfo=SimpleNamespace(model_dump=lambda: {"name": "fixture"}),
        ),
        tools={},
        call=call,
        calls=[],
    )

    @asynccontextmanager
    async def connect(url, client):
        yield mcp

    monkeypatch.setattr(probe, "connect", connect)
    monkeypatch.setattr(probe, "DATA_DIR", tmp_path)
    settings = Settings(xhs_mcp="https://mcp.example")
    args = SimpleNamespace(query="明确查询", explore="主题查询", notes=2, comments=10)
    report = {}
    notes = asyncio.run(collect_xhs(settings, object(), args, report))
    details = [arguments for name, arguments in calls if name == "get_feed_detail"]
    assert [item["feed_id"] for item in details] == ["a", "c"]
    assert all(
        item["limit"] == 10 and item["click_more_replies"] is False for item in details
    )
    assert len(notes) == report["xhs_sample"]["unique_notes_attempted"] == 2
    assert [note["query_kind"] for note in notes] == ["明确目的地", "主题探索"]
    assert report["xhs_sample"]["duplicates_skipped_before_stop"] == 2
    assert report["xhs_sample"]["replies_analysed"] == 0
    assert settings.secrets >= {"token-a", "token-c"}
    checkpoint = (tmp_path / "notes-checkpoint.json").read_text(encoding="utf-8")
    assert "token-a" not in checkpoint and "token-c" not in checkpoint


@pytest.mark.parametrize("pause_after_first", [False, True])
def test_new_run_keeps_only_current_checkpoint_and_clears_old_map(
    monkeypatch, tmp_path, pause_after_first
):
    data_dir = tmp_path / "data"
    export_dir = tmp_path / "reports"
    data_dir.mkdir()
    export_dir.mkdir()
    save_json(
        data_dir / "notes-checkpoint.json",
        [
            {
                "source_id": "old-note",
                "text": "上轮内容",
                "comments": [],
            }
        ],
    )
    save_json(
        export_dir / "map-data.json", {"coordinate_system": "GCJ-02", "old": True}
    )
    settings = Settings(xhs_mcp="https://mcp.example")
    monkeypatch.setattr(Settings, "load", staticmethod(lambda: settings))
    monkeypatch.setattr(probe, "DATA_DIR", data_dir)
    monkeypatch.setattr(probe, "EXPORT_DIR", export_dir)
    detail_calls = []

    async def call(name, arguments):
        if name == "check_login_status":
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="已登录")]
            )
        if name == "search_feeds":
            payload = {
                "feeds": [
                    {"id": "a", "xsecToken": "token-a"},
                    {"id": "b", "xsecToken": "token-b"},
                ]
            }
        else:
            detail_calls.append(arguments["feed_id"])
            if len(detail_calls) > 1:
                raise SourcePaused("验证码")
            payload = {
                "data": {
                    "note": {"title": "本轮内容", "desc": "西湖"},
                    "comments": {"list": [{"id": "comment-a", "content": "好看"}]},
                }
            }
        return SimpleNamespace(structuredContent=payload)

    @asynccontextmanager
    async def connect(url, client):
        if not pause_after_first:
            raise ConnectionError("fixture unavailable")
        yield SimpleNamespace(
            initialized=SimpleNamespace(
                protocolVersion="fixture",
                serverInfo=SimpleNamespace(model_dump=lambda: {}),
            ),
            tools={},
            call=call,
            calls=[],
        )

    @asynccontextmanager
    async def client():
        yield object()

    monkeypatch.setattr(probe, "connect", connect)
    monkeypatch.setattr(probe, "direct_client", client)
    args = SimpleNamespace(
        query="明确查询",
        explore="主题查询",
        notes=5,
        comments=10,
        reuse=False,
        xhs_only=False,
    )
    asyncio.run(probe.run(args))
    samples = json.loads((export_dir / "samples.json").read_text(encoding="utf-8"))
    report = json.loads((export_dir / "capabilities.json").read_text(encoding="utf-8"))
    checkpoint = json.loads(
        (data_dir / "notes-checkpoint.json").read_text(encoding="utf-8")
    )
    assert not (export_dir / "map-data.json").exists()
    assert samples == [
        {key: value for key, value in note.items() if key != "images"}
        for note in checkpoint
    ]
    assert "old-note" not in json.dumps(samples)
    assert report["xhs_failure"]["reason"] == (
        "SourcePaused" if pause_after_first else "ConnectionError"
    )
    if pause_after_first:
        assert len(samples) == 1
        assert report["xhs_sample"]["notes_read"] == 1
        assert report["xhs_sample"]["unique_notes_attempted"] == 2
        assert report["xhs_sample"]["comments_selected"] == 1
        assert report["xhs_sample"]["replies_analysed"] == 0
    else:
        assert samples == []
