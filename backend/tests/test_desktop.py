import sys
from pathlib import Path

import httpx
import pytest

from easytrip.desktop import local_server, single_instance


@pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop lock")
def test_desktop_lock_rejects_second_instance_and_releases(tmp_path):
    with single_instance(tmp_path):
        with pytest.raises(RuntimeError, match="已在运行"):
            with single_instance(tmp_path):
                pytest.fail("duplicate desktop opened")
    with single_instance(tmp_path):
        pass


def test_desktop_server_uses_persistent_directory_and_shuts_down(tmp_path):
    with local_server(tmp_path) as url:
        with httpx.Client(trust_env=False, follow_redirects=True) as client:
            origin = url.split("/desktop/")[0]
            assert client.get(origin + "/api/settings").status_code == 403
            client.get(url)
            response = client.get(origin + "/api/settings")
            assert response.status_code == 200
            assert Path(response.json()["config_path"]) == tmp_path / ".env"
            assert (tmp_path / "easytrip.sqlite3").is_file()
            assert client.get(origin + "/p0/map").status_code == 200
    with httpx.Client(trust_env=False, timeout=1) as client:
        with pytest.raises(httpx.TransportError):
            client.get(origin + "/api/settings")
