# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

python3t_dll = Path(sys.base_prefix) / "abi3t-compat" / "python3t.dll"
binaries = [(str(python3t_dll), ".")] if python3t_dll.is_file() else []

a = Analysis(
    ["v6.py"],
    pathex=[],
    binaries=binaries,
    datas=[("pub.html", "."), ("publicite.gif", ".")],
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
    name="Miclon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
