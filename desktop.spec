# Build separately on Windows and Linux; the output includes its own Python runtime.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, copy_metadata

root = Path(SPECPATH)
datas, binaries, hiddenimports = [], [], []
for package in ("labmcp_epics", "labmcp", "fastmcp", "mcp", "caproto", "jsonschema", "jsonschema_specifications"):
    data, binary, hidden = collect_all(package, filter_submodules=lambda name: not any(part in name for part in (".cli", ".tests", ".test_")))
    datas += data
    binaries += binary
    hiddenimports += hidden
for distribution in ("labmcp-epics", "mcp", "fastmcp", "caproto", "jsonschema"):
    datas += copy_metadata(distribution, recursive=True)
datas += [(str(root / "epics_field_client" / "web" / name), "epics_field_client/web")
          for name in ("index.html", "app.js", "style.css")]
datas += [(str(root / "THIRD_PARTY_NOTICES.txt"), "."),
          (str(root / "third-party-licenses"), "third-party-licenses")]

a = Analysis([str(root / "desktop_entry.py")], pathex=[str(root)], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports,
             excludes=["tkinter", "matplotlib", "scipy", "pandas", "pytest", "IPython", "readline", "curses", "_curses"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="EPICS-Field-Client",
          console=True, debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="EPICS-Field-Client")
