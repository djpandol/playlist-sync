import os, shutil, sys, struct, json, importlib.util, tempfile
from pathlib import Path
UP = Path("/mnt/user-data/uploads")

def load_app():
    spec = importlib.util.spec_from_file_location("playlist_sync", str(Path(__file__).resolve().parent.parent / "playlist_sync.py"))
    m = importlib.util.module_from_spec(spec); sys.modules["playlist_sync"] = m; spec.loader.exec_module(m); return m

def make_env(tmp):
    tmp = Path(tmp)
    ser = tmp / "_Serato_"; shutil.copytree(UP / "_Serato_", ser)
    rb = tmp / "rb"; rb.mkdir()
    for f in ("master.db", "masterPlaylists6.xml"):
        shutil.copy2(UP / "rekordbox" / f, rb / f); os.chmod(rb / f, 0o644)
    os.chmod(ser / "database V2", 0o644)
    for f in (ser / "Subcrates").iterdir(): os.chmod(f, 0o644)
    os.chmod(ser / "neworder.pref", 0o644)
    return ser, rb / "master.db"

def make_stub(path, size=2048):
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_bytes(b"RIFF" + struct.pack("<I", size - 8) + b"WAVE" + b"\0" * (size - 12))
