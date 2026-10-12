import asyncio
from uuid import uuid4

from easytrip.agent.discovery import DiscoveryRunner, DiscoveryStopped
from easytrip.config import Settings
from easytrip.domain.models import CreateTrip
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import Database


def test_runner_routes_search_and_fetch_from_persisted_stage_inputs(tmp_path, monkeypatch):
    async def check():
        database = Database(tmp_path / "routing.sqlite3")
        database.initialize()
        trip = database.create_trip(CreateTrip(operation_id=uuid4()))
        database.enqueue_run(trip["id"], CreateRun(operation_id=uuid4(), kind="discover", expected_version=1))
        run = database.claim_run()
        plan = [{"query": "杭州一日游", "purpose": "main"}]
        database.commit_discovery_stage(run["id"], run["generation"], "understanding", {"query_plan": plan}, 0)
        seen = []

        class Collector:
            def __init__(self, runner, current, client):
                assert current["id"] == run["id"]
                assert not client.trust_env

            async def discover(self, query_plan):
                seen.append(query_plan)
                return {"feeds": [{"external_id": "n1"}], "coverage": {"unique": 1}}

            async def fetch(self, feeds):
                seen.append(feeds)
                return {"notes_obtained": 1}

        monkeypatch.setattr("easytrip.agent.discovery.CollectionService", Collector)
        async def boundary(*args):
            raise DiscoveryStopped("collection_boundary")

        runner = DiscoveryRunner(database, Settings(), stages={"extracting": boundary})
        await runner.execute(database.get_run(run["id"]))
        saved = database.get_run(run["id"])
        assert seen == [plan, [{"external_id": "n1"}]]
        assert saved["checkpoint"]["completed_stages"] == ["understanding", "discovering", "fetching"]
        assert saved["checkpoint"]["fetching"]["notes_obtained"] == 1
        assert saved["status"] == "partial" and saved["stop_reason"] == "collection_boundary"
    asyncio.run(check())
