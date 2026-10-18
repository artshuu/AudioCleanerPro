# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

project_dir = Path(SPECPATH)
datas = []
binaries = []
hiddenimports = []

vosk_datas, vosk_binaries, vosk_hidden = collect_all("vosk")
datas += vosk_datas
binaries += vosk_binaries
hiddenimports += vosk_hidden

words_file = project_dir / "words.txt"
if not words_file.exists():
    raise SystemExit("words.txt не найден в корне проекта.")
datas.append((str(words_file), "."))

model_dir = None
for model_name in ("vosk-model-ru-0.42", "vosk-model-ru-0.22", "vosk-model-small-ru-0.22"):
    candidate = project_dir / model_name
    if (candidate / "am").exists():
        model_dir = candidate
        break
if model_dir is not None:
    datas.append((str(model_dir), model_dir.name))
else:
    print("WARNING: Vosk model not found; censoring will be disabled in the build.")

datas += collect_data_files("customtkinter")
datas += collect_data_files("silero_vad")

a = Analysis(
    [str(project_dir / "app.py")],
    pathex=[str(project_dir)],
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
    name='AudioCleanerPro',
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AudioCleanerPro',
)
app = BUNDLE(
    coll,
    name='AudioCleanerPro.app',
    icon=None,
    bundle_identifier='com.artshuu.audiocleanerpro',
    info_plist={
        'CFBundleDisplayName': 'AudioCleanerPro',
        'CFBundleShortVersionString': '3.8',
        'CFBundleVersion': '3.8',
        'LSMinimumSystemVersion': '11.0',
        'LSApplicationCategoryType': 'public.app-category.utilities',
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
    },
)
