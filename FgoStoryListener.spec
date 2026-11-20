# -*- mode: python ; coding: utf-8 -*-
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

project = Path(SPECPATH)
macos = sys.platform == 'darwin'
script = project / 'fgo_story_listener.py'
runtime_files = (
    ('frida-server-17.17.0-android-arm64',) if macos else
    ('frida-server-17.17.0-android-x86_64', 'frida-gadget-17.17.0-android-arm64.so')
)
datas = [
    (str(project / 'hook.js'), '.'),
    (str(project / 'memory'), 'memory'),
    (str(project / 'translation_runtime'), 'translation_runtime'),
    *((str(project / 'runtime' / name), 'runtime') for name in runtime_files),
]
binaries = []
hiddenimports = []
tmp_ret = collect_all('frida')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    [str(script)],
    pathex=[str(project)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='FgoStoryListener',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=not macos,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='arm64' if macos else None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=not macos,
    upx_exclude=[],
    name='FgoStoryListener',
)

if macos:
    version = re.search(r'^APP_VERSION = "([^"]+)"', script.read_text(encoding='utf-8'), re.M).group(1)
    app = BUNDLE(
        coll,
        name='FgoStoryListener.app',
        bundle_identifier='com.aruke05.fgostorylistener',
        version=version,
        info_plist={'NSHighResolutionCapable': True},
    )
