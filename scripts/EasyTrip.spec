# Only application resources are included; .env, databases and reports stay local.
from pathlib import Path

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / "scripts" / "desktop_entry.py")],
    pathex=[str(root / "backend")],
    datas=[
        (str(root / "frontend" / "dist"), "frontend/dist"),
        (str(root / "backend" / "easytrip" / "storage" / "migrations"), "easytrip/storage/migrations"),
        (str(root / "backend" / "easytrip" / "static"), "easytrip/static"),
    ],
    hiddenimports=["uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on"],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "pytest", "tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name="EasyTrip",
    console=False, debug=False, strip=False, upx=False,
)
