# ----------------------------------------------------------------------------
# ENGINE : Serato reader/writer, rekordbox reader/writer, sync planner/applier
# ----------------------------------------------------------------------------
import os, sys, re, json, struct, time, shutil, hashlib, unicodedata, subprocess, datetime, logging
from pathlib import Path

APP_NAME = "Playlist Sync"
VERSION = "0.1.0"
HOME = Path.home()
DATA_DIR = Path(os.environ.get("PLAYLIST_SYNC_DATA", str(HOME / ".playlist_sync")))
CASEFOLD = sys.platform in ("darwin", "win32")
SEP = "%%"  # Serato sub-crate separator


class SyncError(Exception):
    pass


def nfc(s):
    return unicodedata.normalize("NFC", s)


def pkey(p):
    """Identity key of a file path: Unicode-NFC (+case-insensitive on macOS/Windows)."""
    p = nfc(str(p))
    return p.casefold() if CASEFOLD else p


def now_ts():
    return time.time()


def fmt_len(sec):
    try:
        sec = int(round(float(sec)))
    except Exception:
        return ""
    return "%d:%02d" % (sec // 60, sec % 60)


# ---------------------------------------------------------------- process checks
def _ps_names():
    try:
        out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=5).stdout
        return out.splitlines()
    except Exception:
        return []


def serato_running():
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=5).stdout.lower()
            return "serato dj pro.exe" in out
        except Exception:
            return False
    return any(l.strip().endswith("/Serato DJ Pro") or l.strip() == "Serato DJ Pro" for l in _ps_names())


def rekordbox_running():
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=5).stdout.lower()
            return "rekordbox.exe" in out
        except Exception:
            return False
    for l in _ps_names():
        l = l.strip()
        if l.endswith("/rekordbox") or l == "rekordbox":
            return True
    return False


# ---------------------------------------------------------------- Serato
def _blocks(b):
    i, n = 0, len(b)
    while i + 8 <= n:
        tag = b[i:i + 4].decode("latin1")
        ln = struct.unpack(">I", b[i + 4:i + 8])[0]
        yield tag, b[i + 8:i + 8 + ln]
        i += 8 + ln


def _blk(tag, payload):
    return tag.encode("latin1") + struct.pack(">I", len(payload)) + payload


def _u16(s):
    return s.encode("utf-16-be")


def _s16(b):
    return b.decode("utf-16-be", "replace").rstrip("\x00")


_DEFAULT_COLS = [("song", "343"), ("artist", "117"), ("bpm", "0"), ("key", "92"),
                 ("album", "0"), ("length", "0"), ("comment", "159"), ("filename", "0")]


def _default_crate_header():
    out = _blk("vrsn", _u16("1.0/Serato ScratchLive Crate"))
    for name, width in _DEFAULT_COLS:
        out += _blk("ovct", _blk("tvcn", _u16(name)) + _blk("tvcw", _u16(width)))
    return out


def default_serato_root():
    if sys.platform == "win32":
        return HOME / "Music" / "_Serato_"
    return HOME / "Music" / "_Serato_"


def _camelot_to_key(k):
    m = {"1A": "G#m", "2A": "D#m", "3A": "A#m", "4A": "Fm", "5A": "Cm", "6A": "Gm", "7A": "Dm", "8A": "Am",
         "9A": "Em", "10A": "Bm", "11A": "F#m", "12A": "C#m",
         "1B": "B", "2B": "F#", "3B": "C#", "4B": "G#", "5B": "D#", "6B": "A#", "7B": "F", "8B": "C",
         "9B": "G", "10B": "D", "11B": "A", "12B": "E"}
    return m.get(k.strip().upper())


class Serato:
    def __init__(self, root=None):
        self.root = Path(root) if root else default_serato_root()
        self.sub = self.root / "Subcrates"
        self._lib = None

    def available(self):
        return self.sub.is_dir()

    # -- library database
    def read_library(self, force=False):
        """pkey(abs path) -> metadata dict (from 'database V2')."""
        if self._lib is not None and not force:
            return self._lib
        lib = {}
        f = self.root / "database V2"
        if f.is_file():
            d = f.read_bytes()
            for tag, v in _blocks(d):
                if tag != "otrk":
                    continue
                r = {}
                for t, vv in _blocks(v):
                    if t in ("pfil", "tsng", "tart", "talb", "tgen", "tkey", "tcom", "ttyr", "tlen", "tbpm", "tbit", "tsmp", "tgrp"):
                        r[t] = _s16(vv)
                if "pfil" in r:
                    p = "/" + r["pfil"]
                    lib[pkey(p)] = dict(path=p, title=r.get("tsng", ""), artist=r.get("tart", ""), album=r.get("talb", ""),
                                        genre=r.get("tgen", ""), key=r.get("tkey", ""), comment=r.get("tcom", ""),
                                        year=r.get("ttyr", ""), length=r.get("tlen", ""), bpm=r.get("tbpm", ""),
                                        bitrate=r.get("tbit", ""), samplerate=r.get("tsmp", ""))
        self._lib = lib
        return lib

    # -- crates
    def crate_files(self):
        out = {}
        if self.sub.is_dir():
            for f in sorted(self.sub.iterdir()):
                if f.is_file() and f.name.endswith(".crate") and not f.name.startswith("."):
                    out[nfc(f.name[:-6])] = f
        return out

    def read_crate_paths(self, f):
        d = Path(f).read_bytes()
        paths = []
        for tag, v in _blocks(d):
            if tag == "otrk":
                for t, vv in _blocks(v):
                    if t == "ptrk":
                        paths.append("/" + _s16(vv).lstrip("/"))
        return paths

    def read_crates(self):
        """name(NFC) -> dict(paths=[...], mtime=float, file=Path)"""
        out = {}
        for name, f in self.crate_files().items():
            out[name] = dict(paths=self.read_crate_paths(f), mtime=f.stat().st_mtime, file=f)
        return out

    def _header_for(self, existing_file):
        if existing_file and Path(existing_file).is_file():
            d = Path(existing_file).read_bytes()
            hdr = b"".join(_blk(t, v) for t, v in _blocks(d) if t != "otrk")
            if hdr:
                return hdr
        for f in self.crate_files().values():  # reuse the user's own column layout
            d = f.read_bytes()
            hdr = b"".join(_blk(t, v) for t, v in _blocks(d) if t != "otrk")
            if hdr:
                return hdr
        return _default_crate_header()

    def write_crate(self, name, abs_paths):
        """Create or replace a crate. Existing header (columns/sort) is preserved."""
        self.sub.mkdir(parents=True, exist_ok=True)
        existing = self.crate_files().get(nfc(name))
        target = existing if existing else self.sub / (nfc(name) + ".crate")
        hdr = self._header_for(existing)
        body = b"".join(_blk("otrk", _blk("ptrk", _u16(p.lstrip("/")))) for p in abs_paths)
        tmp = target.with_name(target.name + ".tmp_sync")
        tmp.write_bytes(hdr + body)
        os.replace(tmp, target)
        if not existing:
            self._neworder_add(nfc(name))
        return target

    def remove_crate_to(self, name, backup_dir):
        f = self.crate_files().get(nfc(name))
        if f:
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(Path(backup_dir) / f.name))
        self._neworder_remove(nfc(name))

    def _neworder_remove(self, name):
        f = self.root / "neworder.pref"
        if not f.is_file():
            return
        txt = f.read_bytes().decode("utf-16-be", "replace")
        nl = "\r\n" if "\r\n" in txt else "\n"
        lines = txt.split(nl)
        keep = [l for l in lines if nfc(l) != "[crate]" + name]
        if len(keep) != len(lines):
            f.write_bytes(nl.join(keep).encode("utf-16-be"))

    # neworder.pref keeps the displayed order of crates
    def _neworder_add(self, name):
        f = self.root / "neworder.pref"
        if not f.is_file():
            return
        txt = f.read_bytes().decode("utf-16-be", "replace")
        nl = "\r\n" if "\r\n" in txt else "\n"
        lines = txt.split(nl)
        want = "[crate]" + name
        if any(nfc(l) == want for l in lines):
            return
        # put parent crates' children after the parent block: simply before [end record]
        try:
            idx = max(i for i, l in enumerate(lines) if l.strip() == "[end record]")
        except ValueError:
            return
        lines.insert(idx, want)
        f.write_bytes(nl.join(lines).encode("utf-16-be"))


# ---------------------------------------------------------------- rekordbox
def default_rekordbox_db():
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", HOME)) / "Pioneer" / "rekordbox" / "master.db"
    return HOME / "Library" / "Pioneer" / "rekordbox" / "master.db"


def _import_pyrekordbox():
    try:
        logging.getLogger("pyrekordbox").setLevel(logging.ERROR)
        from pyrekordbox import Rekordbox6Database
        return Rekordbox6Database
    except ImportError:
        raise SyncError("pyrekordbox 가 없습니다.  터미널에서:  python3 -m pip install pyrekordbox")


class Rekordbox:
    SPECIAL_NAMES = {"CUE Analysis Playlist"}

    def __init__(self, dbpath=None):
        self.path = Path(dbpath) if dbpath else default_rekordbox_db()
        self.db = None

    def available(self):
        return self.path.is_file()

    def open(self):
        if self.db is None:
            D = _import_pyrekordbox()
            try:
                self.db = D(path=str(self.path))
            except Exception as e:
                raise SyncError("rekordbox DB를 열 수 없습니다: %s" % e)
        return self.db

    def close(self):
        if self.db is not None:
            try:
                self.db.close()
            except Exception:
                pass
            self.db = None

    def _special_ids(self):
        try:
            from pyrekordbox.db6.database import SPECIAL_PLAYLIST_IDS
            return {str(x) for x in SPECIAL_PLAYLIST_IDS}
        except Exception:
            return set()

    def tree(self):
        """List of node dicts with NFC path-key."""
        db = self.open()
        sp = self._special_ids()
        pls = [p for p in db.get_playlist().all() if str(p.ID) not in sp and nfc(p.Name) not in self.SPECIAL_NAMES]
        by_parent = {}
        for p in pls:
            by_parent.setdefault(str(p.ParentID), []).append(p)
        nodes = []

        def walk(pid, parts):
            for p in sorted(by_parent.get(pid, []), key=lambda x: x.Seq):
                np_ = parts + [nfc(p.Name)]
                kind = {0: "playlist", 1: "folder"}.get(p.Attribute, "smart")
                nodes.append(dict(id=str(p.ID), parent=pid, name=nfc(p.Name), key="/".join(np_), parts=np_,
                                  kind=kind, mtime=p.updated_at.timestamp() if p.updated_at else 0, obj=p))
                if kind == "folder":
                    walk(str(p.ID), np_)

        walk("root", [])
        return nodes

    def songs(self, playlist_id):
        db = self.open()
        from pyrekordbox.db6 import tables
        q = db.query(tables.DjmdSongPlaylist).filter_by(PlaylistID=str(playlist_id)).order_by(tables.DjmdSongPlaylist.TrackNo)
        return q.all()

    def playlist_rows(self, playlist_id):
        rows, mt = [], 0
        for s in self.songs(playlist_id):
            c = s.Content
            if c is None:
                continue
            rows.append(self._row(c))
            if s.updated_at:
                mt = max(mt, s.updated_at.timestamp())
        return rows, mt

    @staticmethod
    def _row(c):
        key = ""
        try:
            key = c.Key.ScaleName if c.Key else ""
        except Exception:
            pass
        try:
            artist = c.Artist.Name if c.Artist else ""
        except Exception:
            artist = ""
        return dict(path=c.FolderPath, title=c.Title or "", artist=artist or "", bpm=(c.BPM or 0) / 100.0,
                    key=key or "", length=c.Length or 0, comment=c.Commnt or "", album="", content_id=str(c.ID))

    def content_index(self):
        """pkey(path) -> DjmdContent"""
        db = self.open()
        idx = {}
        for c in db.get_content().all():
            if c.FolderPath:
                idx[pkey(c.FolderPath)] = c
        return idx
