# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from PyInstaller.utils.hooks import collect_submodules

# Add src to sys.path so PyInstaller can find telewatch
sys.path.insert(0, os.path.abspath('src'))

block_cipher = None

hidden_imports = [
    'openai',
    'anthropic',
    'groq',
    'requests',
    'rich',
    'typer',
    'yaml',
    'dotenv',
    'psutil',
    'watchdog',
    'telegram',
] + collect_submodules('telewatch') + collect_submodules('rich') + collect_submodules('watchdog') + collect_submodules('telegram')

a = Analysis(
    ['src/telewatch/main.py'],
    pathex=['src'],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='telewatch',
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
