"""Keep optional dependencies and all runtime writes inside this project."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
VENDOR = ROOT / ".vendor"
if VENDOR.exists():
    sys.path.insert(0, str(VENDOR))


def configure_runtime():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    for name, folder in {
        "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data",
        "XDG_CACHE_HOME": "cache", "HF_HOME": "huggingface",
        "PLAYWRIGHT_BROWSERS_PATH": "browsers", "TEMP": "tmp", "TMP": "tmp",
    }.items():
        path = RUNTIME / folder
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
