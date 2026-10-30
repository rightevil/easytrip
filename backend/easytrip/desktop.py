"""Windows desktop host. Bundled resources are read-only; user data lives outside the exe."""

import argparse
import ctypes
import os
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from .config import ROOT_DIR, Settings
from .main import create_app


@contextmanager
def single_instance(directory: Path):
    import msvcrt

    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "desktop.lock").open("a+b") as lock:
        lock.seek(0, 2)
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("EasyTrip 已在运行，请切换到已打开的窗口。") from None
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def local_server(directory: Path):
    token = secrets.token_urlsafe(32)
    settings_path = directory / ".env"
    app = create_app(
        Settings.load(settings_path, isolated=True),
        export_dir=directory / "reports" / "p0",
        database_path=directory / "easytrip.sqlite3",
        settings_path=settings_path,
        desktop_token=token,
    )
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", log_config=None, access_log=False,
        loop="asyncio", http="h11", ws="none", timeout_graceful_shutdown=10,
    ))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 30
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("本机服务启动失败，请检查数据目录权限或数据库版本。")
                time.sleep(0.05)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}/desktop/{token}"
        finally:
            server.should_exit = True
            thread.join(timeout=15)


def main():
    parser = argparse.ArgumentParser(description="EasyTrip desktop")
    parser.add_argument("--data-dir", type=Path, default=(
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "EasyTrip"
    ))
    options = parser.parse_args()
    try:
        if not (ROOT_DIR / "frontend" / "dist" / "index.html").is_file():
            raise RuntimeError("网页界面尚未构建，请先运行桌面打包脚本。")
        import webview

        with single_instance(options.data_dir), local_server(options.data_dir) as url:
            webview.settings["ALLOW_FILE_URLS"] = False
            webview.create_window(
                "EasyTrip", url, width=1440, height=960, min_size=(900, 650),
                confirm_close=True,
            )
            webview.start(
                gui="edgechromium", private_mode=True,
                storage_path=str(options.data_dir / "webview"),
                localization={"global.quitConfirmation": "关闭 EasyTrip？已保存的行程会保留，正在运行的任务会中断。"},
            )
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else (
            f"桌面程序启动失败（{type(error).__name__}）。请确认已安装 Microsoft Edge WebView2 运行时，"
            "且本机数据目录可写。"
        )
        ctypes.windll.user32.MessageBoxW(None, message, "EasyTrip", 0x10)


if __name__ == "__main__":
    main()
