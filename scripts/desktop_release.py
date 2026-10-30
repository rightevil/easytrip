"""Verify the desktop bundle and produce versioned, public release assets."""

import hashlib
import os
import shutil
import tomllib
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader


def prepare(root: Path):
    version = tomllib.loads((root / ".cz.toml").read_text(encoding="utf-8"))["tool"]["commitizen"]["version"]
    project = tomllib.loads((root / "backend/pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((root / "backend/uv.lock").read_text(encoding="utf-8"))
    locked_version = next(item["version"] for item in lock["package"] if item["name"] == "easytrip")
    if version != project["version"] or version != locked_version:
        raise ValueError("Commitizen, project and lock versions must agree")
    tag = os.environ.get("GITHUB_REF", "")
    if tag.startswith("refs/tags/") and tag != f"refs/tags/v{version}":
        raise ValueError("Release tag does not match the configured version")

    executable = root / "dist/EasyTrip.exe"
    archive = CArchiveReader(str(executable))
    names = {name.replace("\\", "/") for name in archive.toc}
    if "frontend/dist/index.html" not in names:
        raise ValueError("Frontend missing from desktop bundle")
    for name in names:
        path = Path(name)
        if path.name in {".env", "cookies.json"} or path.suffix in {".sqlite3", ".log"}:
            raise ValueError("Private runtime file found in desktop bundle")
    migrations = (root / "backend/easytrip/storage/migrations").glob("*.sql")
    if any(f"easytrip/storage/migrations/{path.name}" not in names for path in migrations):
        raise ValueError("Database migrations missing from desktop bundle")

    output = root / "dist/release"
    output.mkdir(parents=True, exist_ok=True)
    asset = output / f"EasyTrip-{version}-windows-x64.exe"
    shutil.copyfile(executable, asset)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {asset.name}\n", encoding="ascii")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    marker = f"## v{version} "
    start = changelog.find(marker)
    if start < 0:
        raise ValueError("Current version missing from changelog")
    end = changelog.find("\n## ", start + 1)
    notes = changelog[start:end if end >= 0 else None]
    notes += "\n\n## Windows 使用\n\n下载 exe 后双击启动；需 Microsoft Edge WebView2 运行时。\n"
    notes += "左侧设置支持导入原 .env；保存后重启生效。小红书和 12306 MCP 需单独启动。\n"
    notes += "配置与行程默认保存在 %LOCALAPPDATA%/EasyTrip，更换 exe 不会删除数据。\n"
    notes += "程序不包含个人密钥或行程。HTTP 429 等模型服务限制仍需按服务提示处理。\n"
    (output / "release-notes.md").write_text(notes, encoding="utf-8")
    print(f"Verified release assets: {asset.name}")
    return asset


if __name__ == "__main__":
    prepare(Path(__file__).resolve().parents[1])
