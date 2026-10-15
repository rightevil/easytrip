"""Local readiness checks; configuration values and user records never leave this process."""

import argparse
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .config import DATA_DIR, ROOT_DIR, Settings, validate_url
from .storage.maintenance import MaintenanceError, inspect_database


def readiness(settings=None, *, frontend_dir=None, database_path=None):
    settings = settings or Settings.load()
    frontend = (
        Path(frontend_dir)
        if frontend_dir is not None
        else ROOT_DIR / "frontend" / "dist"
    )
    path = (
        Path(database_path)
        if database_path is not None
        else DATA_DIR / "easytrip.sqlite3"
    )
    database = {"state": "not_created"}
    if path.exists():
        try:
            database = {"state": "ready", **inspect_database(path)}
        except (MaintenanceError, OSError):
            database = {"state": "invalid"}
    dependencies = {}
    for name in ("fastapi", "uvicorn", "httpx", "mcp", "jsonschema", "python-dotenv"):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = None

    def service(address, configured):
        if not address and not configured:
            return "unconfigured"
        if not address or not configured:
            return "incomplete"
        try:
            validate_url(address)
        except ValueError:
            return "invalid"
        return "configured_not_probed"

    built = (frontend / "index.html").is_file() and any(
        (frontend / "assets").glob("*.js")
    )
    return {
        "ready_for_local_workspace": built
        and database["state"] != "invalid"
        and all(dependencies.values()),
        "frontend_built": built,
        "database": database,
        "dependencies": dependencies,
        "services": {
            "xhs_mcp": service(settings.xhs_mcp, bool(settings.xhs_mcp)),
            "rail_mcp": service(settings.rail_mcp, bool(settings.rail_mcp)),
            "model": service(
                settings.model_base_url,
                bool(settings.model_api_key and settings.model_id),
            ),
            "amap_web": "configured_not_probed"
            if settings.amap_web_key
            else "unconfigured",
            "amap_js": "configured_not_probed"
            if settings.amap_js_key and settings.amap_js_secure_key
            else "incomplete"
            if settings.amap_js_key or settings.amap_js_secure_key
            else "unconfigured",
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description="Check local workspace readiness without contacting providers"
    )
    parser.add_argument("--quiet", action="store_true")
    options = parser.parse_args()
    try:
        result = readiness()
    except (OSError, ValueError):
        print(
            "Local readiness check failed; inspect local configuration and permissions."
        )
        return 1
    if not options.quiet:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    if not result["ready_for_local_workspace"]:
        print(
            "Workspace is not ready. Run scripts/dev.ps1 check, then setup/build or check the database."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
