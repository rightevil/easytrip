"""Agnes SSE framing, bounded repair, and interrupted-call accounting."""

import asyncio
import json

import httpx
import pytest

from easytrip.config import Settings
from easytrip.integrations.model import structured_call

USAGE = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
THINKING = "private-thinking-must-not-be-stored"


def event(payload):
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\r\n\r\n"


def choice(delta=None, finish_reason=None):
    return event(
        {
            "choices": [
                {"index": 0, "delta": delta or {}, "finish_reason": finish_reason}
            ]
        }
    )


def usage_event(usage=None):
    return event({"choices": [], "usage": USAGE if usage is None else usage})


def complete(answer='{"ok":true}', *, usage=True):
    parts = [
        choice({"role": "assistant"}),
        choice({"content": answer}),
        choice(finish_reason="stop"),
    ]
    if usage:
        parts.append(usage_event())
    return b"".join(parts) + b"data: [DONE]\r\n\r\n"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks, self.error, self.closed = chunks, error, False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


def response(chunks, error=None):
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream; charset=utf-8"},
        stream=Chunks(chunks, error),
    )


def settings():
    return Settings(
        model_base_url="https://model.example/v1",
        model_api_key="test-secret",
        model_id="agnes-3.0-flash",
    )


async def invoke(client, *, before=None, after=None, **kwargs):
    return await structured_call(
        client,
        settings(),
        system="Return JSON.",
        text_input="Bounded source text.",
        validator=json.loads,
        task_type="evidence_text",
        before_request=before,
        after_request=after,
        **kwargs,
    )


def run(responses, **kwargs):
    calls, reservations, records = [], [], []

    def handle(request):
        calls.append(request)
        assert len(calls) <= len(responses), "unexpected extra physical request"
        return responses[len(calls) - 1]

    async def before(info):
        reservations.append(info)
        return len(reservations)

    async def after(action_id, record):
        records.append((action_id, record))

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), trust_env=False
        ) as client:
            return await invoke(client, before=before, after=after, **kwargs)

    return asyncio.run(check()), calls, reservations, records


def test_real_agnes_stream_shape_and_reasoning_stays_private():
    data = b"".join(
        [
            b": heartbeat\r\n\r\n",
            choice({"role": "assistant", "content": ""}),
            choice({"reasoning_content": THINKING}),
            choice({"content": '{"city":'}),
            choice({"content": '"杭州"}'}),
            choice(finish_reason="stop"),
            usage_event(
                {**USAGE, "completion_tokens_details": {"reasoning_tokens": 35}}
            ),
            b"data: [DONE]\r\n\r\n",
        ]
    )
    # Split event JSON, CRLF boundaries, and UTF-8 code points across transport chunks.
    chunks = [data[index : index + 5] for index in range(0, len(data), 5)]
    result, calls, _, records = run([response(chunks)])
    assert result["status"] == "passed" and result["data"] == {"city": "杭州"}
    assert result["attempts"] == len(calls) == 1
    assert all(result["usage"][key] == value for key, value in USAGE.items())
    assert result["usage_complete"] is True
    metadata = records[0][1]
    assert metadata["reasoning_returned"] is True and metadata["reasoning_tokens"] == 35
    assert metadata["finish_reason"] == "stop" and metadata["result_unknown"] is False
    assert THINKING not in json.dumps([result, records])
    assert "reasoning_content" not in json.dumps([result, records])
    body = json.loads(calls[0].content)
    assert body["stream"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert calls[0].extensions["timeout"]["read"] == 300


def test_complete_stream_without_usage_has_known_answer_and_unknown_cost():
    result, calls, _, records = run([response([complete(usage=False)])])
    assert result["status"] == "passed" and result["data"] == {"ok": True}
    assert result["usage"] == {} and result["usage_complete"] is False
    assert records[0][1]["result_unknown"] is False
    assert records[0][1]["usage_complete"] is False
    assert len(calls) == 1 and result["cost_cny"] is None


@pytest.mark.parametrize("end", ["eof", "timeout", "no_finish_reason"])
def test_unterminated_or_disconnected_stream_is_unknown_without_repair(end):
    data = choice({"content": '{"ok":true}'}) + usage_event()
    if end != "no_finish_reason":
        data += choice(finish_reason="stop")
    else:
        data += b"data: [DONE]\r\n\r\n"
    error = httpx.ReadError("disconnected") if end == "timeout" else None
    result, calls, _, records = run([response([data], error)])
    assert result["status"] == "failed" and result["data"] is None
    assert result["attempts"] == len(calls) == 1
    assert records[0][1]["result_unknown"] is True
    # Usage already received belongs to the physical call even if its answer is unknown.
    assert all(result["usage"][key] == value for key, value in USAGE.items())
    assert records[0][1]["usage_complete"] is True


@pytest.mark.parametrize(
    "bad_event",
    [
        b"data: not-json\r\n\r\n",
        b'event: error\r\ndata: {"error":{"message":"private-provider-error"}}\r\n\r\n',
        event(
            {"error": {"code": "provider_failure", "message": "private-provider-error"}}
        ),
        choice({"content": {"invalid": "content must be text"}}),
    ],
)
def test_invalid_stream_or_provider_error_is_unknown_and_never_retried(bad_event):
    result, calls, _, records = run([response([usage_event(), bad_event])])
    assert result["status"] == "failed" and result["data"] is None
    assert result["attempts"] == len(calls) == 1
    assert records[0][1]["result_unknown"] is True
    assert all(result["usage"][key] == value for key, value in USAGE.items())
    assert "private-provider-error" not in json.dumps([result, records])


@pytest.mark.parametrize(
    "failure",
    [
        b'event: error\r\ndata: {"message":"private-provider-error"}\r\n\r\n',
        event({"error": {}}),
    ],
)
def test_named_or_empty_provider_error_does_not_become_a_success(failure):
    result, calls, _, records = run([response([usage_event(), failure, complete()])])
    assert result["status"] == "failed" and result["data"] is None
    assert len(calls) == 1 and records[0][1]["result_unknown"] is True
    assert result["usage"] == USAGE
    assert "private-provider-error" not in json.dumps([result, records])


@pytest.mark.parametrize("limit", ["answer", "response"])
def test_stream_size_limits_stop_without_repair_or_storing_reasoning(limit):
    payload = (
        choice({"content": "x" * (1024 * 1024 + 1)})
        if limit == "answer"
        else choice({"reasoning_content": "x" * (8 * 1024 * 1024)})
    )
    result, calls, _, records = run([response([usage_event(), payload])])
    assert result["status"] == "failed" and len(calls) == 1
    assert records[0][1]["result_unknown"] is True and result["usage"] == USAGE
    assert len(json.dumps([result, records])) < 10000


def test_multiline_event_and_done_without_a_final_blank_line():
    payload = json.dumps(
        {"choices": [{"index": 0, "delta": {"content": '{"ok":true}'}}]},
        indent=2,
    )
    data = "".join("data: " + line + "\n" for line in payload.splitlines()).encode()
    data += b"\n" + choice(finish_reason="stop") + usage_event() + b"data: [DONE]"
    result, calls, _, _ = run([response([data])])
    assert result["status"] == "passed" and result["data"] == {"ok": True}
    assert len(calls) == 1 and result["usage"] == USAGE


def test_no_newline_stream_is_bounded_before_transport_reads_past_limit():
    block = b"x" * (256 * 1024)
    stream = response(
        [usage_event(), b"data: ", *([block] * 33)],
        RuntimeError("must stop before reading the stream tail"),
    )
    result, calls, _, records = run([stream])
    assert result["status"] == "failed" and len(calls) == 1
    assert records[0][1]["error"] == "RemoteProtocolError"
    assert records[0][1]["result_unknown"] is True and result["usage"] == USAGE


def test_crlf_bytes_are_counted_before_newline_translation():
    line = b":" + b"x" * 1020 + b"\r\n"
    data = line * ((8 * 1024 * 1024) // len(line) + 1)
    result, calls, _, records = run([response([usage_event(), data, complete()])])
    assert result["status"] == "failed" and len(calls) == 1
    assert records[0][1]["result_unknown"] is True and result["usage"] == USAGE


def test_single_cr_line_endings_across_chunks_are_accepted():
    data = complete().replace(b"\r\n", b"\r")
    result, calls, _, _ = run([response([bytes([value]) for value in data])])
    assert result["status"] == "passed" and result["data"] == {"ok": True}
    assert len(calls) == 1 and result["usage"] == USAGE


def test_invalid_utf8_stream_is_unknown_without_repair():
    result, calls, _, records = run([response([usage_event(), b"data: \xff\n\n"])])
    assert result["status"] == "failed" and len(calls) == 1
    assert records[0][1]["result_unknown"] is True and result["usage"] == USAGE


@pytest.mark.parametrize("limit", ["answer", "response"])
def test_json_compatibility_response_keeps_the_same_size_limits(limit):
    message = (
        {"content": "x" * (1024 * 1024 + 1)}
        if limit == "answer"
        else {"reasoning_content": "x" * (8 * 1024 * 1024), "content": '{"ok":true}'}
    )
    result, calls, _, records = run(
        [httpx.Response(200, json={"choices": [{"message": message}], "usage": USAGE})]
    )
    assert result["status"] == "failed" and len(calls) == 1
    assert records[0][1]["result_unknown"] is True
    assert result["usage"] == (USAGE if limit == "answer" else {})
    assert len(json.dumps([result, records])) < 10000


@pytest.mark.parametrize("content", [b"not-json", b"\xff"])
def test_malformed_json_wire_response_is_not_a_business_output_repair(content):
    result, calls, _, records = run(
        [
            httpx.Response(
                200, content=content, headers={"Content-Type": "application/json"}
            )
        ]
    )
    assert result["status"] == "failed" and len(calls) == 1
    assert records[0][1]["result_unknown"] is True
    assert result["usage"] == {} and result["usage_complete"] is False


def test_one_text_repair_keeps_image_fee_once_and_does_not_resend_images():
    image = {"source_id": "note-a", "image_index": 2}
    result, calls, reservations, records = run(
        [response([complete("not valid JSON")]), response([complete()])],
        content=[
            {"type": "text", "text": "Source."},
            {
                "type": "image_url",
                "image_url": {"url": "https://sns-img.xhscdn.com/test.jpg"},
            },
        ],
        images_submitted=1,
        submitted_images=[image],
    )
    assert result["status"] == "passed" and result["attempts"] == len(calls) == 2
    assert result["images_submitted"] == 1 and result["submitted_images"] == [image]
    assert [info["images_submitted"] for info in reservations] == [1, 0]
    assert records[0][1]["result_unknown"] is False
    assert records[1][1]["model_data"] == {"ok": True}
    assert records[1][1]["image_context"] == [image]
    assert all(result["usage"][key] == value * 2 for key, value in USAGE.items())
    repair = json.loads(calls[1].content)
    assert all(isinstance(message["content"], str) for message in repair["messages"])
    assert "image_url" not in json.dumps(repair) and "xhscdn" not in json.dumps(repair)


def test_regular_json_response_remains_compatible_when_stream_was_requested():
    result, calls, _, records = run(
        [
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"ok":true}',
                                "reasoning_content": THINKING,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": USAGE,
                },
            )
        ]
    )
    assert result["status"] == "passed" and result["data"] == {"ok": True}
    assert len(calls) == 1 and result["usage_complete"] is True
    assert records[0][1]["reasoning_returned"] is True
    assert THINKING not in json.dumps([result, records])


def test_cancellation_preserves_received_usage_and_discards_partial_answer_and_thinking():
    async def check():
        waiting = asyncio.Event()
        records, calls = [], []

        class BlockingStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield usage_event() + choice(
                    {"reasoning_content": THINKING, "content": '{"unfinished":'}
                )
                waiting.set()
                await asyncio.Future()

            async def aclose(self):
                self.closed = True

        stream = BlockingStream()

        def handle(request):
            calls.append(request)
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=stream
            )

        async def before(info):
            return "physical-call-1"

        async def after(action_id, record):
            records.append((action_id, record))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), trust_env=False
        ) as client:
            task = asyncio.create_task(invoke(client, before=before, after=after))
            await asyncio.wait_for(waiting.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(calls) == len(records) == 1 and stream.closed
        action_id, record = records[0]
        assert action_id == "physical-call-1" and record["error"] == "CancelledError"
        assert record["result_unknown"] is True and record["reasoning_returned"] is True
        assert record["usage_complete"] is True and record["usage"] == USAGE
        assert "model_data" not in record and "unfinished" not in json.dumps(record)
        assert THINKING not in json.dumps(record)

    asyncio.run(check())
