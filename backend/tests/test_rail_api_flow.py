from datetime import datetime, timedelta

from test_p5_api_flow import adopt, patch_request, plan, prepare
from test_p5_api_flow import workspace as p5_workspace
from test_places import raw_poi

workspace = p5_workspace


def test_confirmed_rail_round_trip_keeps_station_sides_and_reopens_immutable_versions(
    workspace,
):
    open_client, pois, calls = workspace
    pois[0].update(cityname="上海市", location="121.31,31.21", adname="上海区域")
    pois.extend(
        [
            raw_poi(
                "station-hz",
                "杭州东站",
                type="交通设施服务;火车站",
                location="120.2,30.2",
            ),
            raw_poi(
                "station-sh",
                "上海虹桥站",
                cityname="上海市",
                type="交通设施服务;火车站",
                location="121.3,31.2",
            ),
        ]
    )
    with open_client() as client:
        url, identifiers, windows = prepare(client, pois, 1)
        request = client.get(url).json()["request"]
        start = datetime.fromisoformat(windows[0]["start_at"])
        request["planning"]["return_to_origin"] = True
        request["planning"]["rail_segments"] = [
            {
                "id": identifier,
                "date": windows[0]["date"],
                "departure_station_id": identifiers[departure],
                "arrival_station_id": identifiers[arrival],
                "train_number": "用户确认" + identifier,
                "departure_at": (start + timedelta(hours=hour)).isoformat(),
                "arrival_at": (start + timedelta(hours=hour + 1)).isoformat(),
                "confirmed": True,
            }
            for identifier, departure, arrival, hour in [
                ("outbound", "station-hz", "station-sh", 1),
                ("return", "station-sh", "station-hz", 6),
            ]
        ]
        patch_request(client, url, request)
        frozen = client.get(url + "/planning-input").json()
        assert len(frozen["stations"]) == 2 and len(frozen["station_candidates"]) == 2
        saved, _ = plan(client, url, calls)
        assert saved["status"] == "conditional", saved["payload"]["conflicts"]
        assert [step["kind"] for step in saved["payload"]["itinerary"]].count(
            "rail"
        ) == 2
        assert (
            len(
                [
                    step
                    for step in saved["payload"]["itinerary"]
                    if step["kind"] == "route"
                ]
            )
            == 4
        )
        assert adopt(client, url, saved)["trip"]["active_plan_id"] == saved["id"]
        request["planning"]["rail_segments"][1]["departure_at"] = (
            start + timedelta(hours=7)
        ).isoformat()
        request["planning"]["rail_segments"][1]["arrival_at"] = (
            start + timedelta(hours=8)
        ).isoformat()
        patch_request(client, url, request)
        revised, _ = plan(client, url, calls, parent=saved["id"])
        assert (
            revised["parent_plan_id"] == saved["id"]
            and revised["input_hash"] != saved["input_hash"]
        )
        assert client.get(url + f"/plans/{saved['id']}").json() == saved
        assert adopt(client, url, revised)["trip"]["active_plan_id"] == revised["id"]
        before = len(calls)
    with open_client() as client:
        assert client.get(url).json()["active_plan_id"] == revised["id"]
        assert client.get(url + f"/plans/{saved['id']}").json() == saved
        assert len(calls) == before
