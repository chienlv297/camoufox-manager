# PyInstaller spec for the standalone desktop app.
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")
WEBUI = os.path.join(SRC, "camoufox_pm", "webui")
ICON = os.path.join(ROOT, "assets", "app.ico")
VERSION_FILE = os.environ.get("CPM_VERSION_FILE")
BUNDLED_CAMOUFOX = os.environ.get("CPM_BUNDLED_CAMOUFOX_DIR")
APP_DIR_NAME = "CamoufoxProfileManager" if sys.platform == "win32" else "camoufox-pm"
APP_EXE_NAME = "CamoufoxProfileManager" if sys.platform == "win32" else "camoufox-pm"

datas = [(WEBUI, "camoufox_pm/webui"), (ICON, "assets")]
if BUNDLED_CAMOUFOX and os.path.isdir(BUNDLED_CAMOUFOX):
    datas.append((BUNDLED_CAMOUFOX, "camoufox-bundle"))

binaries = []
hiddenimports = []
for package in (
    "camoufox",
    "browserforge",
    "apify_fingerprint_datapoints",
    "language_tags",
    "ua_parser",
    "webview",
) + (("tzdata",) if sys.platform == "win32" else ()):
    package_data, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_hiddenimports

for package in ("uvicorn", "fastapi", "starlette", "webview.platforms"):
    hiddenimports += collect_submodules(package)

a = Analysis(
    [os.path.join(SPECPATH, "launch.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    noarchive=False,
)
pyz = PYZ(a.pure)
exe_kwargs = {
    "name": APP_EXE_NAME,
    "console": sys.platform != "win32",
    "upx": False,
    "icon": ICON if sys.platform == "win32" and os.path.isfile(ICON) else None,
}
if VERSION_FILE and os.path.isfile(VERSION_FILE):
    exe_kwargs["version"] = VERSION_FILE

exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_kwargs)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_DIR_NAME, upx=False)
app = BUNDLE(
    coll,
    name="Camoufox Profile Manager.app",
    bundle_identifier="com.github.polyackiy.camoufox-pm",
)
