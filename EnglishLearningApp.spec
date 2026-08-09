# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "audio"), "audio"),
    (str(ROOT / "course_audio"), "course_audio"),
    (str(ROOT / "data"), "data"),
    (str(ROOT / "templates"), "templates"),
]
for name in ("README_VI.txt", "THIRD_PARTY_NOTICES.txt"):
    p = ROOT / name
    if p.exists():
        datas.append((str(p), "."))

# pywebview's official PyInstaller hook collects the Windows DLLs it needs.
# Excluding unused Qt/CEF backends keeps this Edge/WebView2 build smaller.
a = Analysis(
    [str(ROOT / "desktop_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3",
        "matplotlib", "numpy", "pandas", "pytest", "tkinter",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="English Learning App",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "app.ico") if (ROOT / "app.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="English Learning App",
)
