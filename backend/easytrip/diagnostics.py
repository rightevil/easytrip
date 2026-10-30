"""Local readiness checks; configuration values and user records never leave this process."""

import argparse
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .config import DATA_DIR, ROOT_DIR, Settings, configuration_checks
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
            name: check["state"] for name, check in configuration_checks(vars(settings)).items()
            if name != "amap_mcp"
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
