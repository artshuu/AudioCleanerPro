# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

project_dir = Path(SPECPATH)
datas = []

words_file = project_dir / "words.txt"
if not words_file.exists():
    raise SystemExit("words.txt не найден в корне проекта — без словаря цензура не работает.")
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

# customtkinter грузит темы/шрифты как файлы данных.
datas += collect_data_files("customtkinter")

# Silero VAD загружает .jit через importlib.resources — данные обязательны.
datas += collect_data_files("silero_vad")

a = Analysis(
    [str(project_dir / "app.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
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
    [],
    exclude_binaries=True,
    name='AudioCleanerPro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name='AudioCleanerPro',
)
