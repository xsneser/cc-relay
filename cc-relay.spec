# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for CC Relay (Single-file Windows Executable)

import os
import sys

block_cipher = None

# 项目根目录
BASE_DIR = os.path.abspath(os.getcwd())

datas = [
    ('ui.html', '.'),
    ('config.example.json', '.'),
    ('docs/THIRD_PARTY_NOTICES.md', 'docs'),
]

# 如果存在 custom_modifier.py 等可选模块，确保打包
hiddenimports = [
    'cc_relay',
    'relay',
    'relay.constants',
    'relay.config',
    'relay.traffic',
    'relay.router',
    'relay.modifier',
    'relay.audit',
    'relay.voice_bridge',
    'relay.voice_context',
    'relay.lifecycle',
    'relay.adapters',
    'relay.adapters.openai_adapter',
    'relay.upstreams',
    'relay.upstreams.manager',
    'relay.upstreams.antigravity_updater',
    'relay.upstreams.cpa_updater',
    'relay.upstreams.relay_updater',
    'relay.upstreams.codex_login',
    'relay.upstreams.codex_callback',
    'relay.handlers.proxy_handler',
    'relay.handlers.ui_handler',
    'custom_modifier',
    'lifecycle',
    'urllib.request',
    'urllib.error',
    'http.server',
    'json',
    'socket',
    'threading',
    'webbrowser',
    'ctypes',
]

a = Analysis(
    ['main_launcher.py'],
    pathex=[BASE_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy', 'torch', 'torchaudio', 'torchvision', 'transformers', 'modelscope', 'accelerate', 'funasr', 'sherpa_onnx', 'PIL', 'Pillow'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='cc-relay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 用户选择: 后台静默无黑框弹窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
