# PyInstaller one-file build. Runtime data and credentials are never bundled.
from pathlib import Path

APP = Path(SPECPATH).resolve()
ROOT = APP.parent

a = Analysis(
    [str(APP / "windows_entrypoint.py")],
    pathex=[str(APP)],
    binaries=[],
    datas=[(str(APP / "web"), "web")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PelicanWatch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
)
