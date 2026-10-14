import asyncio

import httpx
import pytest

from easytrip.integrations.amap import Amap, AmapBudgetExceeded, AmapError


def test_candidate_search_exhausts_bounded_pages_and_uses_page_count():
    requests = []

    def handler(request):
        requests.append(request)
        page = int(request.url.params["page_num"])
        rows = [{"id": f"B{page}-{index}"} for index in range(20 if page == 1 else 1)]
        return httpx.Response(
            200, json={"status": "1", "count": str(len(rows)), "pois": rows}
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            amap = Amap(client, "test-only")
            result = await amap.search_candidates("西湖", "杭州")
            assert result["search_complete"] is True and len(result["pois"]) == 21
            assert result["api_version"] == "v5"
            assert len(amap.calls) == 2

    asyncio.run(run())
    assert [request.url.params["page_num"] for request in requests] == ["1", "2"]
    assert all(request.url.params["page_size"] == "20" for request in requests)


@pytest.mark.parametrize("case", ["full", "duplicate", "bad-count", "missing-id"])
def test_partial_duplicate_and_malformed_search_never_claim_complete(case):
    def handler(request):
        page = int(request.url.params["page_num"])
        rows = [{"id": f"B{page}-{index}"} for index in range(20)]
        count = "20"
        if case == "duplicate" and page == 2:
            rows, count = [{"id": "B1-0"}], "1"
        if case == "bad-count":
            rows, count = [{"id": "B1"}], "999"
        if case == "missing-id":
            rows, count = [{"name": "无ID"}], "1"
        return httpx.Response(200, json={"status": "1", "count": count, "pois": rows})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert (await Amap(client, "test-only").search_candidates("西湖", "杭州"))[
                "search_complete"
            ] is False

    asyncio.run(run())


def test_physical_request_budget_stops_inside_pagination_and_before_detail():
    physical_calls = []

    def handler(request):
        physical_calls.append(request)
        return httpx.Response(
            200,
            json={
                "status": "1",
                "count": "20",
                "pois": [{"id": f"B{index}"} for index in range(20)],
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            amap = Amap(client, "test-only", max_calls=40)
            amap.calls.extend(["prior-call"] * 39)
            with pytest.raises(AmapBudgetExceeded):
                await amap.search_candidates("西湖", "杭州")
            with pytest.raises(AmapBudgetExceeded):
                await amap.detail("B0")
            assert len(amap.calls) == 40 and len(physical_calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "kwargs", [{"city": ""}, {"page_size": 26}, {"page_size": True}, {"page_num": 0}]
)
def test_invalid_city_or_pagination_does_not_call_provider(kwargs):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("No upstream call")
            )
        ) as client:
            with pytest.raises(ValueError):
                await Amap(client, "test-only").search("西湖", **kwargs)

    asyncio.run(run())


@pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError])
def test_cancelled_or_failed_request_gate_never_counts_a_physical_request(error):
    async def gate():
        await asyncio.sleep(0)
        raise error()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("No upstream call")
            )
        ) as client:
            amap = Amap(client, "test-only", before_request=gate)
            with pytest.raises(error):
                await amap.detail("B001")
            assert amap.calls == []

    asyncio.run(run())


def test_request_budget_is_rechecked_after_concurrent_gate_waits():
    physical_calls = []

    def handler(request):
        physical_calls.append(request)
        return httpx.Response(200, json={"status": "1", "pois": []})

    async def run():
        release = asyncio.Event()
        entered = 0

        async def gate():
            nonlocal entered
            entered += 1
            if entered == 2:
                release.set()
            await release.wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            amap = Amap(client, "test-only", max_calls=1, before_request=gate)
            results = await asyncio.gather(
                amap.detail("B001"), amap.detail("B002"), return_exceptions=True
            )
            assert (
                sum(isinstance(result, AmapBudgetExceeded) for result in results) == 1
            )
            assert len(physical_calls) == len(amap.calls) == 1

    asyncio.run(run())


def test_qps_limit_is_reported_without_an_automatic_retry():
    physical_calls = []

    def handler(request):
        physical_calls.append(request)
        return httpx.Response(200, json={"status": "0", "infocode": "10021"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            amap = Amap(client, "test-only")
            with pytest.raises(AmapError) as error:
                await amap.detail("B001")
            assert error.value.infocode == "10021"
            assert len(physical_calls) == len(amap.calls) == 1

    asyncio.run(run())
