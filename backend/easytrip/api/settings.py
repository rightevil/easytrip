"""Edit local configuration without returning stored credentials to the UI."""

import hashlib
import io
import json
import os
import tempfile
from pathlib import Path

from dotenv.parser import parse_stream
from fastapi import APIRouter, HTTPException, Request, Response

from ..config import (
    ALIASES,
    Settings,
    configuration_checks,
    normalize_settings,
    settings_overrides,
    validate_setting,
)

FIELDS = tuple(name for name in Settings.__dataclass_fields__ if name != "secrets")


def setup_required(path: Path):
    return not path.exists() and not path.with_name("setup-complete").exists()


def imported_values(content):
    if not isinstance(content, str) or len(content.encode("utf-8")) > 65536 or "\ufffd" in content:
        raise ValueError("请选择不超过 64 KB 的 UTF-8 配置文件")
    raw = {}
    known = {name.upper() for name in FIELDS} | set(ALIASES.values())
    for binding in parse_stream(io.StringIO(content.lstrip("\ufeff"))):
        if binding.error:
            raise ValueError(f"配置文件第 {binding.original.line} 行格式有误，请检查引号和等号")
        if binding.key is not None:
            name = binding.key.upper()
            if name in known and name in raw and raw[name] != binding.value:
                raise ValueError(f"{name} 重复且值不同，请先保留一个值")
            raw[name] = binding.value
    # Empty imported values preserve existing keys; environment interpolation is never run.
    values = normalize_settings({key: value for key, value in raw.items() if value and value.strip()})
    if not values:
        raise ValueError("文件中没有可导入的非空 EasyTrip 配置")
    return values, sum(key not in known for key in raw), sum(
        key in known and not (value or "").strip() for key, value in raw.items()
    )


async def payload_from(request: Request, keys: set[str]):
    if request.headers.get("x-easytrip-settings") != "1":
        raise HTTPException(403, "请从应用设置页操作配置")
    try:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 262144:
                raise ValueError
        payload = json.loads(body)
        if not isinstance(payload, dict) or set(payload) != keys:
            raise ValueError
        return payload
    except (ValueError, TypeError):
        raise HTTPException(400, "设置请求格式无效或文件过大") from None


def changes_from(payload):
    changes = payload["changes"]
    if not isinstance(changes, dict) or not set(changes) <= set(FIELDS) or any(
        not isinstance(value, str) or len(value) > 4096 for value in changes.values()
    ):
        raise HTTPException(400, "配置字段或内容格式无效")
    return changes


def validate_changes(changes):
    for name, value in changes.items():
        try:
            validate_setting(name, value)
        except ValueError:
            raise HTTPException(400, f"{name.upper()} 格式无效；地址须为 HTTP(S) URL，内容不能包含换行或无效字符") from None


def saved_values(settings: Settings, path: Path):
    values = {name: getattr(settings, name) for name in FIELDS}
    values.update(settings_overrides(path))
    return values


def write_settings(path: Path, values: dict[str, str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Replace atomically: a failed write must leave the previous credentials intact.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            for name in FIELDS:
                escaped = values[name].replace("\\", "\\\\").replace("'", "\\'")
                output.write(f"{name.upper()}='{escaped}'\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def make_router(settings: Settings, path: Path):
    router = APIRouter(prefix="/api/settings")

    def snapshot():
        values = saved_values(settings, path)
        return {
            "fields": {
                name: {"configured": bool(value), "value": value if name == "model_id" else ""}
                for name, value in values.items()
            },
            "revision": hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
            "restart_required": any(values[name] != getattr(settings, name) for name in FIELDS),
            "config_path": str(path),
            "checks": configuration_checks(values),
            "setup_required": setup_required(path),
        }

    def current_values(revision):
        if revision != snapshot()["revision"]:
            raise HTTPException(409, "设置已在其他窗口更新，请重新读取后修改")
        return saved_values(settings, path)

    @router.get("")
    async def read_settings(response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            return snapshot()
        except (OSError, UnicodeError):
            raise HTTPException(500, "无法读取本机设置文件") from None

    @router.put("")
    async def update_settings(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        # Parse here so validation errors never echo submitted keys or service URLs.
        payload = await payload_from(request, {"revision", "changes"})
        changes = changes_from(payload)
        validate_changes(changes)
        try:
            values = current_values(payload["revision"])
            values.update({name: value.strip() for name, value in changes.items()})
            write_settings(path, values)
            return snapshot()
        except (OSError, UnicodeError):
            raise HTTPException(500, "设置未保存，请检查本机配置目录的写入权限") from None

    @router.post("/check")
    async def check_settings(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        payload = await payload_from(request, {"revision", "changes"})
        changes = changes_from(payload)
        try:
            values = current_values(payload["revision"])
            values.update(changes)
            return {"checks": configuration_checks(values)}
        except (OSError, UnicodeError):
            raise HTTPException(500, "无法读取本机设置文件") from None

    @router.post("/import")
    async def import_settings(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        payload = await payload_from(request, {"revision", "content", "apply"})
        try:
            if type(payload["apply"]) is not bool:
                raise ValueError("导入操作无效")
            changes, ignored, empty = imported_values(payload["content"])
        except UnicodeError:
            raise HTTPException(400, "配置文件编码无效，请使用 UTF-8") from None
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        validate_changes(changes)
        try:
            values = current_values(payload["revision"])
            values.update(changes)
            result = {
                "imported_fields": list(changes), "ignored_fields": ignored, "skipped_empty": empty,
                "checks": configuration_checks(values),
            }
            if payload["apply"]:
                write_settings(path, values)
                result["settings"] = snapshot()
            return result
        except (OSError, UnicodeError):
            raise HTTPException(500, "导入未完成，请检查配置文件与目录的读写权限") from None

    @router.post("/setup-complete")
    async def complete_setup(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        await payload_from(request, set())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.with_name("setup-complete").touch(exist_ok=True)
            return {"setup_required": False}
        except OSError:
            raise HTTPException(500, "无法保存引导状态，请检查配置目录写入权限") from None

    return router
