import asyncio
import json

import httpx
import pytest

from backend.app.config import Settings
from backend.app.integrations.model import extract, image_url


def settings(base="https://model.example/v1"):
    return Settings(
        model_base_url=base, model_api_key="test-secret", model_id="test-vision-model"
    )


def notes():
    return [
        {
            "source_id": "note-1",
            "title": "杭州攻略",
            "text": "西湖适合散步，排队久但夜景好。",
            "comments": [
                {"source_id": "note-1-comment-1", "text": "西湖排队一小时，但夜景值得"},
            ],
            "images": [],
        }
    ]


def extraction():
    return {
        "places": [
            {
                "name": "西湖",
                "city": "杭州",
                "category": "play",
                "source_id": "note-1",
                "evidence": "西湖适合散步",
            }
        ],
        "comments": [
            {
                "source_id": "note-1-comment-1",
                "place_name": "西湖",
                "dimension": "排队/景观",
                "stance": "mixed",
                "evidence": "排队一小时，但夜景值得",
            },
        ],
        "gaps": ["本批样本没有住宿线索"],
    }


def run_extract(responses, sample=None, config=None):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                responses[len(requests) - 1], ensure_ascii=False
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), trust_env=False
        ) as client:
            return await extract(client, config or settings(), sample or notes())

    return asyncio.run(run()), requests


@pytest.mark.parametrize(
    "base", ["https://model.example/v1", "https://model.example/v1/chat/completions"]
)
def test_real_request_shape_and_mixed_comment(base):
    result, requests = run_extract([extraction()], config=settings(base))
    assert result["status"] == "passed"
    assert result["attempts"] == 1
    assert result["extraction"]["comments"][0]["stance"] == "mixed"
    assert str(requests[0].url) == "https://model.example/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer test-secret"
    body = json.loads(requests[0].content)
    assert body["model"] == "test-vision-model"
    assert body["response_format"] == {"type": "json_object"}
    assert "test-secret" not in str(body)
    assert result["extraction"]["image_observations"] == []


@pytest.mark.parametrize(
    "change",
    ["fake_source", "fake_evidence", "fake_place", "extra_field", "coerced_type"],
)
def test_invalid_evidence_or_structure_repairs_once(change):
    invalid = extraction()
    if change == "fake_source":
        invalid["places"][0]["source_id"] = "fabricated-note"
    elif change == "fake_evidence":
        invalid["comments"][0]["evidence"] = "从来没有排队"
    elif change == "fake_place":
        invalid["comments"][0]["place_name"] = "未抽取分店"
    elif change == "extra_field":
        invalid["places"][0]["latitude"] = 30
    else:
        invalid["places"][0]["city"] = 123
    result, requests = run_extract([invalid, extraction()])
    assert result["status"] == "passed"
    assert result["attempts"] == len(requests) == 2
    assert result["usage"] == {
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "total_tokens": 30,
    }
    repair = json.loads(requests[1].content)["messages"][-1]["content"]
    assert "校验未通过" in repair


def test_invalid_evidence_stops_after_one_repair():
    invalid = extraction()
    invalid["places"][0]["evidence"] = "无来源证据"
    result, requests = run_extract([invalid, invalid])
    assert result["status"] == "failed"
    assert result["extraction"] is None
    assert result["attempts"] == len(requests) == 2
    assert "exact excerpt" in result["error"]
    assert "无来源证据" not in result["error"]


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_missing_input_comment_requires_explicit_classification(repair_succeeds):
    sample = notes()
    sample[0]["comments"].append(
        {"source_id": "note-1-comment-3", "text": "灵隐寺在哪约啊"}
    )
    missing = extraction()
    complete = extraction()
    complete["comments"].append(
        {
            "source_id": "note-1-comment-3",
            "place_name": None,
            "dimension": "预约方式",
            "stance": "question",
            "evidence": "灵隐寺在哪约啊",
        }
    )
    result, requests = run_extract(
        [missing, complete if repair_succeeds else missing], sample
    )
    assert result["attempts"] == len(requests) == 2
    assert result["status"] == ("passed" if repair_succeeds else "failed")
    if repair_succeeds:
        assert {item["source_id"] for item in result["extraction"]["comments"]} == {
            "note-1-comment-1",
            "note-1-comment-3",
        }
    else:
        assert result["extraction"] is None


def test_image_filter_and_original_indices_with_three_image_limit():
    sample = notes()
    sample[0]["images"] = [
        "http://sns-webpic-qc.xhscdn.com/image-1.jpg",
        "https://evil.example/image.jpg",
        "https://xhscdn.com.evil.example/image.jpg",
        "https://sns.xhscdn.com/image.jpg?xsec_token=private",
        "https://sns.xhscdn.com/image.jpg?signature=private",
        "https://user:password@sns.xhscdn.com/image.jpg",
        "https://sns.xhscdn.com/image-2.jpg?imageView2=1",
        "https://img.xiaohongshu.com/image-3.jpg",
        "https://sns.xhscdn.com/image-4.jpg",
    ]
    output = extraction()
    output["image_observations"] = [
        {
            "source_id": "note-1",
            "image_index": 1,
            "text": "图片似乎包含路线说明",
            "uncertain": True,
        }
    ]
    result, requests = run_extract([output], sample)
    assert result["images_submitted"] == 3
    body = json.loads(requests[0].content)
    urls = [
        part["image_url"]["url"]
        for part in body["messages"][1]["content"]
        if part["type"] == "image_url"
    ]
    assert urls == [
        "https://sns-webpic-qc.xhscdn.com/image-1.jpg",
        "https://sns.xhscdn.com/image-2.jpg?imageView2=1",
        "https://img.xiaohongshu.com/image-3.jpg",
    ]
    assert "private" not in str(body)
    assert result["extraction"]["image_observations"][0]["uncertain"] is True


def test_image_observation_must_reference_submitted_image_and_repair_is_text_only():
    sample = notes()
    sample[0]["images"] = ["https://sns.xhscdn.com/image-1.jpg"]
    invalid = extraction()
    invalid["image_observations"] = [
        {
            "source_id": "note-1",
            "image_index": 2,
            "text": "不存在的图片",
            "uncertain": False,
        }
    ]
    result, requests = run_extract([invalid, extraction()], sample)
    assert result["status"] == "passed"
    assert result["images_submitted"] == 1
    assert all(
        isinstance(message["content"], str)
        for message in json.loads(requests[1].content)["messages"]
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://[broken",
        "https://sns.xhscdn.com:bad/a",
        "file:///image.jpg",
        "https://sns.xhscdn.com/a#token=private",
    ],
)
def test_malformed_image_urls_are_excluded(url):
    assert image_url(url) is None


def test_http_error_propagates_without_retry():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "unauthorized"})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), trust_env=False
        ) as client:
            await extract(client, settings(), notes())

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    assert len(calls) == 1
