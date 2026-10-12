import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from easytrip.config import Settings, direct_client
from easytrip.integrations.model import extract, image_url


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
        response = responses[len(requests) - 1]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(response, ensure_ascii=False)
                        },
                        "finish_reason": "stop",
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
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert body["max_tokens"] == 16384
    assert requests[0].extensions["timeout"]["read"] == 300
    assert result["usage_complete"] is True
    record = result["requests"][0]
    assert record["task_type"] == "text_extraction"
    assert record["max_tokens"] == body["max_tokens"]
    assert record["thinking_enabled"] is True
    assert record["usage_complete"] is True
    assert record["finish_reason"] == "stop"
    assert record["reasoning_returned"] is False
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
    assert result["usage_complete"] is True
    assert [record["task_type"] for record in result["requests"]] == [
        "text_extraction",
        "output_repair",
    ]
    assert [json.loads(request.content)["max_tokens"] for request in requests] == [
        16384,
        32768,
    ]
    assert all(
        json.loads(request.content)["chat_template_kwargs"] == {"enable_thinking": True}
        for request in requests
    )
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


def test_every_input_comment_can_be_explicit_unknown_or_multiple_dimensions():
    sample = notes()
    sample[0]["comments"].append({"source_id": "note-1-comment-2", "text": "赞"})
    output = extraction()
    output["comments"] += [
        {
            "source_id": "note-1-comment-1",
            "place_name": "西湖",
            "dimension": "景观",
            "stance": "positive",
            "evidence": "夜景值得",
        },
        {
            "source_id": "note-1-comment-2",
            "place_name": None,
            "dimension": "无地点体验信息",
            "stance": "unknown",
            "evidence": "赞",
        },
    ]
    result, requests = run_extract([output], sample)
    assert result["status"] == "passed"
    assert len(requests) == 1
    assert len(result["extraction"]["comments"]) == 3
    assert result["unclassified_comment_ids"] == []
    assert result["extraction"]["comments"][-1]["place_name"] is None
    assert result["extraction"]["comments"][-1]["stance"] == "unknown"


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
    assert body["max_tokens"] == 32768
    assert result["requests"][0]["task_type"] == "multimodal_extraction"
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
    assert [record["task_type"] for record in result["requests"]] == [
        "multimodal_extraction",
        "output_repair",
    ]
    assert all(
        json.loads(request.content)["max_tokens"] == 32768 for request in requests
    )
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


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(401, json={"error": "unauthorized"}),
        httpx.ReadTimeout("https://model.example/?key=test-secret"),
        httpx.ConnectError("https://model.example/?key=test-secret"),
    ],
)
def test_http_error_returns_failed_without_retry_or_credential_message(failure):
    result, requests = run_extract([failure])
    assert result["status"] == "failed"
    assert result["extraction"] is None
    assert result["attempts"] == len(requests) == 1
    assert result["usage"] == {}
    assert result["usage_complete"] is False
    assert result["requests"][0]["usage_complete"] is False
    assert result["requests"][0]["error"] == (
        "HTTPStatusError"
        if isinstance(failure, httpx.Response)
        else type(failure).__name__
    )
    if isinstance(failure, httpx.Response):
        assert result["requests"][0]["http_status"] == 401
    assert "test-secret" not in json.dumps(result)
    assert "model.example" not in json.dumps(result)


def test_repair_timeout_preserves_first_response_usage_without_network_retry():
    invalid = extraction()
    invalid["places"][0]["evidence"] = "编造的证据"
    result, requests = run_extract([invalid, httpx.ReadTimeout("fixture timeout")])
    assert result["status"] == "failed"
    assert result["extraction"] is None
    assert result["attempts"] == len(requests) == 2
    assert result["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert result["usage_complete"] is False
    assert [record["usage_complete"] for record in result["requests"]] == [True, False]
    assert result["requests"][1]["task_type"] == "output_repair"
    assert result["requests"][1]["max_tokens"] == 32768
    assert result["requests"][1]["error"] == "ReadTimeout"
    assert all(
        isinstance(message["content"], str)
        for message in json.loads(requests[1].content)["messages"]
    )


def test_actual_extract_post_ignores_proxy_environment(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(
                (self.path, self.headers["Authorization"], json.loads(body))
            )
            response = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(extraction(), ensure_ascii=False)
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:

        async def run():
            async with direct_client() as client:
                return await extract(
                    client,
                    settings(f"http://127.0.0.1:{server.server_port}/v1"),
                    notes(),
                )

        result = asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert result["status"] == "passed"
    assert result["usage"]["total_tokens"] == 15
    assert len(received) == 1
    path, authorization, body = received[0]
    assert path == "/v1/chat/completions"
    assert authorization == "Bearer test-secret"
    assert body["model"] == "test-vision-model"
    assert "test-secret" not in json.dumps(body)
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert body["max_tokens"] == 16384


def test_reasoning_metadata_is_recorded_without_exporting_reasoning_content():
    response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(extraction()),
                        "reasoning_content": "private reasoning test-secret",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "completion_tokens_details": {"reasoning_tokens": 3},
            },
        },
    )
    result, requests = run_extract([response])
    assert result["status"] == "passed"
    assert len(requests) == 1
    assert result["usage_complete"] is True
    assert result["requests"][0]["reasoning_returned"] is True
    assert result["requests"][0]["reasoning_tokens"] == 3
    assert result["requests"][0]["finish_reason"] == "stop"
    assert result["usage"]["reasoning_tokens"] == 3
    assert "private reasoning" not in json.dumps(result)
    assert "test-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"prompt_tokens": True, "completion_tokens": 5, "total_tokens": 15},
        {"prompt_tokens": 10, "completion_tokens": -1, "total_tokens": 15},
    ],
)
def test_missing_or_invalid_usage_is_reported_incomplete(usage):
    result, requests = run_extract(
        [
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": json.dumps(extraction())},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage,
                },
            )
        ]
    )
    assert result["status"] == "passed"
    assert len(requests) == 1
    assert result["usage_complete"] is False
    assert result["requests"][0]["usage_complete"] is False
    assert all(type(count) is int and count >= 0 for count in result["usage"].values())


@pytest.mark.parametrize("choice", [None, "not an object", {"message": None}])
def test_malformed_choice_repairs_once_without_unhandled_attribute_error(choice):
    malformed = httpx.Response(200, json={"choices": [choice]})
    result, requests = run_extract([malformed, extraction()])
    assert result["status"] == "passed"
    assert result["attempts"] == len(requests) == 2
    assert result["usage_complete"] is False
