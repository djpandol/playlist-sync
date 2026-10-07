#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Playlist Sync 0.1.0 -- two-way playlist sync between Serato DJ Pro and rekordbox.
Non-commercial, free for everyone (MIT).  Run:  python3 playlist_sync.py
"""
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

# ----------------------------------------------------------------------------
# SYNC : planner / applier (stateful three-way, playlist level, order-exact)
# ----------------------------------------------------------------------------
import collections


def load_state():
    f = DATA_DIR / "state.json"
    try:
        return json.loads(f.read_text("utf-8"))
    except Exception:
        return {"version": 1, "base": {}}


def save_state(st):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / "state.json.tmp"
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, DATA_DIR / "state.json")


def _sig(paths):
    return [pkey(p) for p in paths]


def _diff(a, b):
    """a=source list, b=target list (pkey lists) -> summary"""
    ca, cb = collections.Counter(a), collections.Counter(b)
    add = sum((ca - cb).values())
    rem = sum((cb - ca).values())
    order_only = (add == 0 and rem == 0 and a != b)
    return dict(add=add, remove=rem, order_only=order_only)


def _safe_name(s):
    return nfc(s).replace("/", "-").replace("\\", "-").replace(SEP, "-")


class Model:
    def __init__(self, serato_root=None, rb_path=None, state_dir=None):
        self.serato = Serato(serato_root)
        self.rb = Rekordbox(rb_path)
        self.warnings = []

    # ---------------------------------------------------- snapshots
    def serato_nodes(self):
        """Tree nodes + playlists of Serato. Returns (nodes, playlists{key:...})."""
        crates = self.serato.read_crates()
        names = sorted(crates)
        children = collections.defaultdict(set)
        for n in names:
            parts = n.split(SEP)
            for i in range(1, len(parts)):
                children[SEP.join(parts[:i])].add(n)
        nodes, pl = [], {}
        seen = set()

        def add_node(full, parts):
            has_file = full in crates
            has_children = any(k.startswith(full + SEP) for k in names)
            ntracks = len(crates[full]["paths"]) if has_file else 0
            kind = "folder" if (has_children and ntracks == 0) else "playlist"
            node = dict(id=full, key="/".join(parts), name=parts[-1], parts=parts, kind=kind, depth=len(parts) - 1)
            nodes.append(node)
            if kind == "playlist":
                blocked = None
                if has_children:
                    blocked = "하위 크레이트가 있는 크레이트의 곡은 rekordbox 폴더에 둘 수 없어 동기화하지 않습니다"
                pl[node["key"]] = dict(name=full, paths=crates[full]["paths"] if has_file else [],
                                       mtime=crates[full]["mtime"] if has_file else 0, blocked=blocked)

        def rec(prefix_parts):
            prefix = SEP.join(prefix_parts)
            kids = sorted({SEP.join(n.split(SEP)[:len(prefix_parts) + 1]) for n in names
                           if (len(prefix_parts) == 0 or n.startswith(prefix + SEP)) and len(n.split(SEP)) > len(prefix_parts)})
            for full in kids:
                parts = full.split(SEP)
                if full in seen:
                    continue
                seen.add(full)
                add_node(full, parts)
                rec(parts)

        rec([])
        return nodes, pl

    def rb_nodes(self):
        nodes = self.rb.tree()
        pl = {}
        for n in nodes:
            if n["kind"] == "playlist":
                rows, mt = self.rb.playlist_rows(n["id"])
                pl[n["key"]] = dict(id=n["id"], rows=rows, paths=[r["path"] for r in rows],
                                    mtime=max(n["mtime"], mt))
        out = [{k: v for k, v in n.items() if k not in ("obj",)} for n in nodes]
        for o in out:
            o["depth"] = len(o["parts"]) - 1
        return out, pl

    # ---------------------------------------------------- plan
    def plan(self, S=None, R=None):
        base = load_state().get("base", {})
        if S is None:
            _, S = self.serato_nodes()
        if R is None:
            _, R = self.rb_nodes() if self.rb.available() else ([], {})
        items = []
        for key in sorted(set(S) | set(R), key=lambda k: [nfc(x).lower() for x in k.split("/")]):
            s, r = S.get(key), R.get(key)
            it = dict(key=key, serato=None, rekordbox=None, status="", action="skip", note="", diff=None, blocked=None)
            if s:
                it["serato"] = dict(count=len(s["paths"]), mtime=s["mtime"])
            if r:
                it["rekordbox"] = dict(count=len(r["paths"]), mtime=r["mtime"])
            b = base.get(key)
            if s and s.get("blocked"):
                it.update(status="blocked", note=s["blocked"], blocked=s["blocked"])
                items.append(it)
                continue
            if s and r:
                hs, hr = _sig(s["paths"]), _sig(r["paths"])
                if hs == hr:
                    it.update(status="same", action="skip", note="동일")
                else:
                    if b is None:
                        newer = "to_rekordbox" if s["mtime"] >= r["mtime"] else "to_serato"
                        it.update(status="differs", action=newer, note="처음 비교: 서로 다름 (최근 수정된 쪽 기본 선택)")
                        it["diff"] = _diff(hs if newer == "to_rekordbox" else hr, hr if newer == "to_rekordbox" else hs)
                    elif hs == b:
                        it.update(status="rekordbox_changed", action="to_serato", note="rekordbox에서 변경됨")
                        it["diff"] = _diff(hr, hs)
                    elif hr == b:
                        it.update(status="serato_changed", action="to_rekordbox", note="Serato에서 변경됨")
                        it["diff"] = _diff(hs, hr)
                    else:
                        newer = "to_rekordbox" if s["mtime"] >= r["mtime"] else "to_serato"
                        it.update(status="both_changed", action=newer, note="양쪽 모두 변경됨 (최근 수정된 쪽 기본 선택)")
                        it["diff"] = _diff(hs if newer == "to_rekordbox" else hr, hr if newer == "to_rekordbox" else hs)
            elif s:
                if b is None:
                    it.update(status="new_serato", action="to_rekordbox", note="Serato에만 있음 → rekordbox에 새로 만들기")
                else:
                    it.update(status="deleted_in_rekordbox", action="skip", note="rekordbox에서 삭제된 것으로 보임 (삭제 전파는 직접 선택)")
            else:
                if b is None:
                    it.update(status="new_rekordbox", action="to_serato", note="rekordbox에만 있음 → Serato에 새로 만들기")
                else:
                    it.update(status="deleted_in_serato", action="skip", note="Serato에서 삭제된 것으로 보임 (삭제 전파는 직접 선택)")
            items.append(it)
        return items

    # ---------------------------------------------------- apply
    def _backup(self, touch_rb, touch_serato, crate_names):
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        d = DATA_DIR / "backups" / ts
        d.mkdir(parents=True, exist_ok=True)
        if touch_rb and self.rb.path.is_file():
            (d / "rekordbox").mkdir(exist_ok=True)
            for f in (self.rb.path, self.rb.path.with_name("masterPlaylists6.xml")):
                if f.is_file():
                    shutil.copy2(f, d / "rekordbox" / f.name)
        if touch_serato:
            (d / "serato" / "Subcrates").mkdir(parents=True, exist_ok=True)
            cf = self.serato.crate_files()
            for n in crate_names:
                if n in cf:
                    shutil.copy2(cf[n], d / "serato" / "Subcrates" / cf[n].name)
            np_ = self.serato.root / "neworder.pref"
            if np_.is_file():
                shutil.copy2(np_, d / "serato" / np_.name)
        # keep newest 30 backups
        allb = sorted((DATA_DIR / "backups").iterdir())
        for old in allb[:-30]:
            shutil.rmtree(old, ignore_errors=True)
        return d

    def apply(self, decisions, log=lambda m: None):
        """decisions: [{key, action}] ; action in to_rekordbox|to_serato|delete_serato|delete_rekordbox"""
        plan = {it["key"]: it for it in self.plan()}
        todo = []
        for d in decisions:
            it = plan.get(d["key"])
            if not it or d["action"] in ("skip", "", None):
                continue
            if it["blocked"]:
                raise SyncError("동기화할 수 없는 항목: %s (%s)" % (it["key"], it["blocked"]))
            todo.append((it, d["action"]))
        results = []
        touch_rb = any(a in ("to_rekordbox", "delete_rekordbox") for _, a in todo)
        touch_se = any(a in ("to_serato", "delete_serato") for _, a in todo)
        if touch_rb and rekordbox_running():
            raise SyncError("rekordbox가 실행 중입니다. rekordbox를 완전히 종료한 뒤 다시 실행하세요.")
        if touch_se and serato_running():
            raise SyncError("Serato DJ Pro가 실행 중입니다. Serato를 완전히 종료한 뒤 다시 실행하세요.")
        # validate sources before touching anything
        for it, a in todo:
            if a == "to_rekordbox" and not it["serato"]:
                raise SyncError("Serato에 없는 항목을 보낼 수 없습니다: " + it["key"])
            if a == "to_serato" and not it["rekordbox"]:
                raise SyncError("rekordbox에 없는 항목을 보낼 수 없습니다: " + it["key"])
        _, S = self.serato_nodes()
        crate_names = [S[it["key"]]["name"] for it, _ in todo if it["key"] in S]
        bdir = self._backup(touch_rb, touch_se, crate_names)
        log("백업: %s" % bdir)
        st = load_state()
        report = []
        # ---- 1) rekordbox (one transaction; rolled back on any error)
        rb_items = [(it, a) for it, a in todo if a in ("to_rekordbox", "delete_rekordbox")]
        if rb_items:
            self.rb.close()
            db = self.rb.open()
            try:
                idx = self.rb.content_index()
                lib = self.serato.read_library()
                for it, a in rb_items:
                    if a == "to_rekordbox":
                        report.append(self._write_rb_playlist(db, idx, lib, it["key"], S[it["key"]]["paths"], log))
                    else:
                        report.append(self._delete_rb_playlist(db, it["key"], log))
                db.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
                self.rb.close()
                raise
            self.rb.close()
        # ---- 2) serato (plain files; each crate written atomically)
        for it, a in todo:
            if a == "to_serato":
                R = self.rb_nodes()[1]
                self.rb.close()
                rows = R[it["key"]]["rows"]
                lib = self.serato.read_library()
                paths = []
                for r in rows:
                    m = lib.get(pkey(r["path"]))
                    paths.append(m["path"] if m else r["path"])
                name = SEP.join(_safe_name(x) for x in it["key"].split("/"))
                self.serato.write_crate(name, paths)
                log("Serato 크레이트 기록: %s (%d곡)" % (name, len(paths)))
                report.append(dict(key=it["key"], action=a, count=len(paths), warnings=[], written=_sig(paths)))
            elif a == "delete_serato":
                self.serato.remove_crate_to(S[it["key"]]["name"], bdir / "serato" / "deleted")
                report.append(dict(key=it["key"], action=a, count=0, warnings=[], written=None))
        # ---- 3) verify by re-reading both libraries from disk
        self.rb.close()
        self.serato._lib = None
        _, S2 = self.serato_nodes()
        _, R2 = self.rb_nodes() if self.rb.available() else ([], {})
        self.rb.close()
        for rp in report:
            k, a = rp["key"], rp["action"]
            if a == "to_rekordbox":
                got = _sig(R2[k]["paths"]) if k in R2 else None
                rp["verified"] = got == rp["written"]
            elif a == "to_serato":
                got = _sig(S2[k]["paths"]) if k in S2 else None
                rp["verified"] = got == rp["written"]
            elif a == "delete_serato":
                rp["verified"] = k not in S2
            else:
                rp["verified"] = k not in R2
            if a in ("to_rekordbox", "to_serato") and rp["verified"] and k in S2 and k in R2 and _sig(S2[k]["paths"]) == _sig(R2[k]["paths"]):
                st["base"][k] = _sig(S2[k]["paths"])
            elif a.startswith("delete") and rp["verified"]:
                st["base"].pop(k, None)
            rp.pop("written", None)
        for k, it in plan.items():
            if it["status"] == "same" and k in S2:
                st["base"][k] = _sig(S2[k]["paths"])
        save_state(st)
        return dict(backup=str(bdir), results=report)

    # -- rekordbox writers
    def _meta_kwargs(self, db, path, m, log):
        kw = {}
        title = (m or {}).get("title") or Path(path).stem
        kw["Title"] = title
        art = (m or {}).get("artist")
        if art:
            a = db.get_artist(Name=art).first()
            kw["ArtistID"] = (a or db.add_artist(art)).ID
        if m:
            if m.get("comment"):
                kw["Commnt"] = m["comment"]
            try:
                if m.get("bpm"):
                    kw["BPM"] = int(round(float(m["bpm"]) * 100))
            except ValueError:
                pass
            k = (m.get("key") or "").strip()
            if k:
                k2 = _camelot_to_key(k) or k
                kr = db.get_key(ScaleName=k2).first()
                if kr:
                    kw["KeyID"] = kr.ID
            if (m.get("year") or "").strip().isdigit():
                kw["ReleaseYear"] = int(m["year"])
            ln = (m.get("length") or "")
            mm = re.match(r"^(\d+):(\d+(?:\.\d+)?)$", ln.strip())
            if mm:
                kw["Length"] = int(round(int(mm.group(1)) * 60 + float(mm.group(2))))
            mb = re.match(r"^(\d+(?:\.\d+)?)", (m.get("bitrate") or ""))
            if mb:
                kw["BitRate"] = int(float(mb.group(1)))
            ms = re.match(r"^(\d+(?:\.\d+)?)", (m.get("samplerate") or ""))
            if ms:
                v = float(ms.group(1))
                kw["SampleRate"] = int(v * 1000) if v < 1000 else int(v)
        return kw

    def _find_playlist(self, db, parts):
        parent = "root"
        node = None
        for i, nm in enumerate(parts):
            last = i == len(parts) - 1
            cand = [p for p in db.get_playlist(ParentID=parent).all() if nfc(p.Name) == nm and
                    ((p.Attribute == 0) if last else (p.Attribute == 1))]
            if not cand:
                return None, parent, i
            node = cand[0]
            parent = str(node.ID)
        return node, parent, len(parts)

    def _write_rb_playlist(self, db, idx, lib, key, paths, log):
        parts = key.split("/")
        warnings, missing = [], 0
        pl, parent, depth = self._find_playlist(db, parts)
        if pl is None:
            parent_obj = None
            par = "root"
            for nm in parts[:-1]:
                cand = [p for p in db.get_playlist(ParentID=par).all() if nfc(p.Name) == nm and p.Attribute == 1]
                if cand:
                    f = cand[0]
                else:
                    f = db.create_playlist_folder(nm, parent=None if par == "root" else par)
                    log("rekordbox 폴더 생성: %s" % nm)
                par = str(f.ID)
            pl = db.create_playlist(parts[-1], parent=None if par == "root" else par)
            log("rekordbox 플레이리스트 생성: %s" % key)
        else:
            for s in self.rb.songs(pl.ID):
                db.delete(s)
            db.flush()
            log("rekordbox 플레이리스트 갱신: %s" % key)
        cache = {}
        n = 0
        written = []
        for p in paths:
            k = pkey(p)
            c = idx.get(k) or cache.get(k)
            if c is None:
                if not os.path.isfile(p):
                    missing += 1
                    warnings.append("파일 없음(건너뜀): %s" % p)
                    continue
                try:
                    c = db.add_content(p, **self._meta_kwargs(db, p, lib.get(k), log))
                except Exception as e:
                    missing += 1
                    warnings.append("rekordbox에 추가 실패(건너뜀): %s (%s)" % (p, e))
                    continue
                idx[k] = c
            db.add_to_playlist(pl, c)
            written.append(k)
            n += 1
        db.flush()
        return dict(key=key, action="to_rekordbox", count=n, warnings=warnings, skipped_missing=missing, written=written)

    def _delete_rb_playlist(self, db, key, log):
        pl, _, _ = self._find_playlist(db, key.split("/"))
        if pl is not None:
            db.delete_playlist(pl)
            log("rekordbox 플레이리스트 삭제: %s" % key)
        return dict(key=key, action="delete_rekordbox", count=0, warnings=[], written=None)

# ----------------------------------------------------------------------------
# SERVER : tiny local web UI (stdlib only) + CLI
# ----------------------------------------------------------------------------
import threading, secrets, argparse, http.server, socketserver, urllib.parse, webbrowser, io, contextlib

UI_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>Playlist Sync</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0b0c;--line:#2a2a2d;--txt:#d6d6d6;--dim:#8a8a8f;--ok:#3fb950;--warn:#e3a008;--bad:#e5484d;--info:#4a9eff}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--txt);font:12px/1.35 -apple-system,"Apple SD Gothic Neo","Malgun Gothic",Segoe UI,Helvetica,Arial,sans-serif;overflow:hidden}
button,select{font:inherit;color:var(--txt);background:#26262a;border:1px solid #3a3a40;border-radius:4px;padding:4px 10px;cursor:pointer}
button:hover:not(:disabled){background:#33333a}
button:disabled{opacity:.4;cursor:default}
button.primary{background:#1e5fa8;border-color:#2b78cc;color:#fff}
button.primary:hover:not(:disabled){background:#2570c4}
#app{display:grid;grid-template-rows:38px 1fr 230px;height:100%}
header{display:flex;align-items:center;gap:12px;padding:0 12px;background:#141416;border-bottom:1px solid var(--line)}
header h1{font-size:13px;margin:0;font-weight:600;letter-spacing:.3px}
header .sp{flex:1}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--dim)}
.dot{width:8px;height:8px;border-radius:50%;background:#555;display:inline-block;flex:none}
.dot.same{background:var(--ok)}.dot.differs,.dot.both_changed{background:var(--bad)}
.dot.serato_changed,.dot.rekordbox_changed,.dot.new_serato,.dot.new_rekordbox{background:var(--warn)}
.dot.deleted_in_serato,.dot.deleted_in_rekordbox,.dot.blocked{background:#777}
.dot.run{background:var(--bad)}.dot.idle{background:var(--ok)}
main{display:grid;grid-template-columns:1fr 54px 1fr;min-height:0}
.mid{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;background:#101012;border-left:1px solid var(--line);border-right:1px solid var(--line)}
.mid button{width:38px;padding:6px 0;font-size:16px}
.pane{display:grid;grid-template-columns:200px 1fr;grid-template-rows:26px 1fr 22px;min-width:0;min-height:0}
.pane .ph{grid-column:1/3;display:flex;align-items:center;gap:8px;padding:0 10px;font-weight:600;font-size:11px;letter-spacing:.5px;text-transform:uppercase}
.pane .pf{grid-column:1/3;display:flex;align-items:center;padding:0 10px;font-size:11px;gap:14px}
.tree{overflow:auto;padding:4px 0;user-select:none}
.grid{overflow:auto;min-width:0;min-height:0;position:relative}
.node{display:flex;align-items:center;gap:6px;height:22px;padding-right:8px;white-space:nowrap;cursor:default}
.node .ic{width:14px;text-align:center;flex:none;font-size:11px}
.node .nm{overflow:hidden;text-overflow:ellipsis;flex:1}
.node .ct{color:var(--dim);font-size:10px}
.msg{padding:14px;color:var(--dim)}
table{border-collapse:collapse;width:100%;table-layout:fixed}
th{position:sticky;top:0;text-align:left;font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.4px;padding:0 6px;height:22px;white-space:nowrap;overflow:hidden}
td{padding:0 6px;height:22px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td.n,th.n{width:34px;text-align:right}td.bpm,th.bpm{width:44px;text-align:right}td.key,th.key{width:46px}td.len,th.len{width:48px;text-align:right}
td.ar,th.ar{width:26%}td.cm,th.cm{width:20%}
tr.miss td{opacity:.5;text-decoration:line-through}
/* ---- Serato look ---- */
.serato{background:#111}
.serato .ph{background:#1a1a1a;color:#c9c9c9;border-bottom:1px solid #000}
.serato .tree{background:#202020;border-right:1px solid #000}
.serato .node.sel{background:#1e4a77;color:#fff}
.serato .node .ic{color:#f58b1f}
.serato th{background:#262626;color:#9a9a9a;border-bottom:1px solid #000;border-right:1px solid #111}
.serato tbody tr{background:#161616}.serato tbody tr:nth-child(even){background:#1b1b1b}
.serato td{color:#d8d8d8;border-right:1px solid #141414}
.serato td.ti{color:#6fb3ff}
.serato tr.sel td{background:#444}
.serato .pf{background:#1a1a1a;border-top:1px solid #000;color:#9a9a9a}
/* ---- rekordbox look ---- */
.rb{background:#0e0e10}
.rb .ph{background:#232323;color:#bdbdbd;border-bottom:1px solid #000}
.rb .tree{background:#1e1e1e;border-right:1px solid #000}
.rb .node.sel{background:#2d6cdf;color:#fff}
.rb .node .ic{color:#9aa3ad}
.rb th{background:#1b1b19;color:#8d8d8d;border-bottom:1px solid #333;border-right:1px solid #2a2a2a;text-transform:none;font-size:11px;font-weight:400}
.rb tbody tr{background:#0e0e10}.rb tbody tr:nth-child(even){background:#131316}
.rb td{color:#cfcfcf;border-bottom:1px solid #18181b}
.rb tr.sel td{background:#243b66}
.rb .pf{background:#000;border-top:1px solid #222;color:#8d8d8d}
td.mm{box-shadow:inset 3px 0 0 var(--warn)}
.k{font-weight:600}
/* ---- plan ---- */
.plan{display:grid;grid-template-rows:30px 1fr 40px;border-top:1px solid var(--line);background:#141416;min-height:0}
.plan .tabs{display:flex;align-items:flex-end;gap:2px;padding:0 10px;border-bottom:1px solid var(--line)}
.tab{padding:6px 14px;border:1px solid transparent;border-bottom:none;border-radius:4px 4px 0 0;cursor:pointer;color:var(--dim)}
.tab.on{background:#1c1c20;border-color:var(--line);color:var(--txt)}
.plan .body{overflow:auto;background:#1c1c20}
.plan table th{background:#222226;color:#999;border-bottom:1px solid var(--line);font-size:10px}
.plan td{border-bottom:1px solid #26262a}
.plan td.c1,.plan th.c1{width:30px;text-align:center}.plan td.num,.plan th.num{width:70px;text-align:right}.plan td.st,.plan th.st{width:170px}.plan td.ac,.plan th.ac{width:200px}
.plan select{width:100%;padding:1px 4px}
.plan tr.sel td{background:#26303f}
.plan .foot{display:flex;align-items:center;gap:10px;padding:0 12px;border-top:1px solid var(--line)}
.plan .foot .sp{flex:1}
pre.log{margin:0;padding:10px;font:11px/1.45 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;color:#bbb}
.err{color:var(--bad)}.okc{color:var(--ok)}
#modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:10}
#modal .box{background:#1d1d21;border:1px solid #3a3a40;border-radius:8px;padding:18px;width:560px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;gap:12px}
#modal h3{margin:0;font-size:14px}
#modal .mb{overflow:auto;white-space:pre-wrap;line-height:1.5}
#modal .btns{display:flex;gap:8px;justify-content:flex-end}
#modal input{width:100%;background:#111;border:1px solid #3a3a40;color:var(--txt);padding:5px 8px;border-radius:4px;font:inherit}
#modal label{display:block;margin-top:6px;color:var(--dim)}
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>Playlist Sync <span class="pill" id="ver"></span></h1>
    <span class="pill"><i class="dot" id="d-s"></i><span id="t-s">Serato</span></span>
    <span class="pill"><i class="dot" id="d-r"></i><span id="t-r">rekordbox</span></span>
    <span class="sp"></span>
    <span class="pill" id="legend"></span>
    <button id="b-lang">EN</button>
    <button id="b-set"></button>
    <button id="b-ref"></button>
  </header>
  <main>
    <section class="pane serato" id="p-s">
      <div class="ph"><span style="color:#f58b1f">&#9632;</span> Serato DJ Pro</div>
      <div class="tree" id="tr-s"></div><div class="grid" id="gr-s"></div>
      <div class="pf" id="pf-s"></div>
    </section>
    <div class="mid">
      <button id="b-r" title="">&rarr;</button>
      <button id="b-l" title="">&larr;</button>
    </div>
    <section class="pane rb" id="p-r">
      <div class="ph"><span style="color:#2d6cdf">&#9679;</span> rekordbox</div>
      <div class="tree" id="tr-r"></div><div class="grid" id="gr-r"></div>
      <div class="pf" id="pf-r"></div>
    </section>
  </main>
  <section class="plan">
    <div class="tabs"><div class="tab on" id="tab-plan"></div><div class="tab" id="tab-log"></div></div>
    <div class="body" id="body"></div>
    <div class="foot"><span id="sum"></span><span class="sp"></span>
      <button id="b-all"></button><button id="b-none"></button><button class="primary" id="b-run"></button></div>
  </section>
</div>
<div id="modal"><div class="box"><h3 id="m-t"></h3><div class="mb" id="m-b"></div><div class="btns" id="m-x"></div></div></div>
<script>
"use strict";
const TOKEN="__TOKEN__", VERSION="__VERSION__";
const I18N={
ko:{refresh:"새로고침",settings:"설정",sync:"선택 항목 동기화",all:"변경 항목 모두 선택",none:"선택 해제",plan:"동기화 계획",log:"결과",
 pl:"플레이리스트",cs:"Serato",cr:"rekordbox",status:"상태",action:"동작",note:"설명",
 st:{same:"동일",differs:"서로 다름",serato_changed:"Serato 변경",rekordbox_changed:"rekordbox 변경",both_changed:"양쪽 변경",new_serato:"Serato에만 있음",new_rekordbox:"rekordbox에만 있음",deleted_in_serato:"Serato에서 삭제됨?",deleted_in_rekordbox:"rekordbox에서 삭제됨?",blocked:"동기화 불가"},
 ac:{skip:"건너뜀",to_rekordbox:"Serato → rekordbox",to_serato:"rekordbox → Serato",delete_serato:"Serato에서 삭제",delete_rekordbox:"rekordbox에서 삭제"},
 run:"실행 중",idle:"종료됨",empty:"항목이 없습니다",selpl:"플레이리스트를 선택하세요",tracks:"곡",
 toR:"선택한 플레이리스트를 Serato → rekordbox 로 보냅니다",toS:"선택한 플레이리스트를 rekordbox → Serato 로 보냅니다",
 confirm:"실행 확인",cancel:"취소",ok:"실행",close:"닫기",nothing:"선택된 항목이 없습니다.",
 willdo:"다음 작업을 실행합니다. 실행 전에 자동 백업이 만들어집니다.",del:"삭제 항목이 포함되어 있습니다. 삭제된 플레이리스트는 백업 폴더에서 복구할 수 있습니다.",
 done:"완료",fail:"실패",verified:"검증됨",unverified:"검증 실패",tr:"트랙",
 setT:"경로 설정",setS:"Serato 폴더 (_Serato_)",setR:"rekordbox master.db",save:"저장",
 legend:"● 동일  ● 변경  ● 충돌/차이",missing:"파일 없음",newer:"최근 수정"},
en:{refresh:"Refresh",settings:"Settings",sync:"Sync selected",all:"Select all changes",none:"Clear",plan:"Sync plan",log:"Result",
 pl:"Playlist",cs:"Serato",cr:"rekordbox",status:"Status",action:"Action",note:"Note",
 st:{same:"In sync",differs:"Differs",serato_changed:"Serato changed",rekordbox_changed:"rekordbox changed",both_changed:"Both changed",new_serato:"Only in Serato",new_rekordbox:"Only in rekordbox",deleted_in_serato:"Deleted in Serato?",deleted_in_rekordbox:"Deleted in rekordbox?",blocked:"Cannot sync"},
 ac:{skip:"Skip",to_rekordbox:"Serato → rekordbox",to_serato:"rekordbox → Serato",delete_serato:"Delete in Serato",delete_rekordbox:"Delete in rekordbox"},
 run:"running",idle:"closed",empty:"Nothing here",selpl:"Select a playlist",tracks:"tracks",
 toR:"Send selected playlist Serato → rekordbox",toS:"Send selected playlist rekordbox → Serato",
 confirm:"Confirm",cancel:"Cancel",ok:"Run",close:"Close",nothing:"Nothing selected.",
 willdo:"The following will be done. An automatic backup is made first.",del:"Deletions are included. Deleted playlists can be restored from the backup folder.",
 done:"Done",fail:"Failed",verified:"verified",unverified:"verification failed",tr:"tracks",
 setT:"Paths",setS:"Serato folder (_Serato_)",setR:"rekordbox master.db",save:"Save",
 legend:"● in sync  ● changed  ● conflict/differs",missing:"file missing",newer:"newer"}
};
let lang=(()=>{try{return localStorage.getItem("ps_lang")||((navigator.language||"").startsWith("ko")?"ko":"en")}catch(e){return "en"}})();
const T=()=>I18N[lang];
const $=s=>document.getElementById(s);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let snap=null, sel={key:null}, picks={}, tracksCache={s:[],r:[]}, tab="plan", lastLog="";
const CAM={1:"#40e0d0",2:"#5be07a",3:"#9be05b",4:"#d4e05b",5:"#e0b95b",6:"#e08d5b",7:"#e0675b",8:"#e05b8d",9:"#d05be0",10:"#8d5be0",11:"#5b7de0",12:"#5bb5e0"};
function camCol(k){const m=/^(\\d+)[AB]$/.exec(k||"");return m?CAM[+m[1]]:"#aaa"}

async function api(path,opt){
  opt=opt||{};opt.headers=Object.assign({"X-Token":TOKEN},opt.headers||{});
  const r=await fetch(path,opt);const j=await r.json();
  if(!r.ok) throw new Error(j.error||r.status);return j;
}
const q=o=>Object.entries(o).map(([k,v])=>k+"="+encodeURIComponent(v)).join("&");

function modal(title,body,buttons){
  return new Promise(res=>{
    $("m-t").textContent=title;
    if(typeof body==="string")$("m-b").textContent=body;else{$("m-b").innerHTML="";$("m-b").appendChild(body)}
    const x=$("m-x");x.innerHTML="";
    buttons.forEach(b=>{const e=document.createElement("button");e.textContent=b.label;if(b.primary)e.className="primary";
      e.onclick=()=>{$("modal").style.display="none";res(b.value)};x.appendChild(e)});
    $("modal").style.display="flex";
  });
}

function setStatic(){
  const t=T();
  $("ver").textContent="v"+VERSION;$("b-ref").textContent=t.refresh;$("b-set").textContent=t.settings;
  $("b-run").textContent=t.sync;$("b-all").textContent=t.all;$("b-none").textContent=t.none;
  $("tab-plan").textContent=t.plan;$("tab-log").textContent=t.log;$("b-lang").textContent=lang==="ko"?"EN":"한";
  $("b-r").title=t.toR;$("b-l").title=t.toS;
  $("legend").innerHTML='<i class="dot same"></i>'+(lang==="ko"?"동기화됨":"in sync")+' <i class="dot serato_changed"></i>'+(lang==="ko"?"변경/신규":"changed/new")+' <i class="dot differs"></i>'+(lang==="ko"?"차이/충돌":"differs")+' <i class="dot blocked"></i>'+(lang==="ko"?"불가/삭제?":"n/a")
}

function renderTree(side){
  const el=$(side==="s"?"tr-s":"tr-r");
  const d=snap[side==="s"?"serato":"rekordbox"];
  if(!d.available){el.innerHTML='<div class="msg err">'+esc(T().empty)+'</div>';return}
  el.innerHTML="";
  const hidden=new Set();
  d.nodes.forEach(n=>{
    const row=document.createElement("div");row.className="node"+(n.key===sel.key&&n.kind!=="folder"?" sel":"");
    row.style.paddingLeft=(8+n.depth*14)+"px";
    const ic=n.kind==="folder"?"▾":(side==="s"?"■":"≡");
    row.innerHTML='<span class="ic">'+ic+'</span><span class="nm" title="'+esc(n.key)+'">'+esc(n.name)+'</span>'+
      (n.count!=null?'<span class="ct">'+n.count+'</span>':'')+'<i class="dot '+(n.status||"")+'"></i>';
    if(n.kind!=="folder"){row.onclick=()=>selectKey(n.key)}
    el.appendChild(row);
  });
  if(!d.nodes.length)el.innerHTML='<div class="msg">'+esc(T().empty)+'</div>';
}

function renderGrid(side){
  const rows=tracksCache[side],el=$(side==="s"?"gr-s":"gr-r");
  const other=new Set((tracksCache[side==="s"?"r":"s"]).map(x=>x.k));
  if(!sel.key){el.innerHTML='<div class="msg">'+esc(T().selpl)+'</div>';return}
  const oth=tracksCache[side==="s"?"r":"s"];
  let h='<table><thead><tr><th class="n">#</th><th>'+(side==="s"?"Song":"Track Title")+'</th><th class="ar">Artist</th><th class="bpm">BPM</th><th class="key">Key</th><th class="len">Time</th><th class="cm">Comment</th></tr></thead><tbody>';
  rows.forEach((r,i)=>{
    const o=oth[i];const mm=oth.length&&(!o||o.k!==r.k);
    h+='<tr class="'+(r.exists?"":"miss")+'" data-i="'+i+'"><td class="n'+(mm?" mm":"")+'">'+r.n+'</td><td class="ti" title="'+esc(r.path)+'">'+esc(r.title)+'</td><td class="ar">'+esc(r.artist)+'</td><td class="bpm">'+esc(r.bpm)+'</td><td class="key k" style="color:'+camCol(r.key)+'">'+esc(r.key)+'</td><td class="len">'+esc(r.length)+'</td><td class="cm">'+esc(r.comment)+'</td></tr>';
  });
  el.innerHTML=h+"</tbody></table>";
  el.querySelectorAll("tbody tr").forEach(tr=>tr.onclick=()=>{el.querySelectorAll("tr.sel").forEach(x=>x.classList.remove("sel"));tr.classList.add("sel")});
  $(side==="s"?"pf-s":"pf-r").textContent=rows.length+" "+T().tracks;
}

async function selectKey(key){
  sel.key=key;tracksCache={s:[],r:[]};
  const sn=snap.serato.nodes.some(n=>n.key===key&&n.kind!=="folder"),rn=snap.rekordbox.nodes.some(n=>n.key===key&&n.kind!=="folder");
  try{
    const [a,b]=await Promise.all([sn?api("/api/tracks?"+q({side:"serato",key})):[],rn?api("/api/tracks?"+q({side:"rekordbox",key})):[]]);
    tracksCache={s:a,r:b};
  }catch(e){$("body").innerHTML='<pre class="log err">'+esc(e.message)+'</pre>'}
  renderTree("s");renderTree("r");renderGrid("s");renderGrid("r");renderPlan();
  $("pf-s").textContent=sn?tracksCache.s.length+" "+T().tracks:"";$("pf-r").textContent=rn?tracksCache.r.length+" "+T().tracks:"";
}

function actionOptions(it){
  const o=["skip"];
  if(it.blocked)return o;
  if(it.serato)o.push("to_rekordbox");
  if(it.rekordbox)o.push("to_serato");
  if(it.status==="deleted_in_rekordbox")o.push("delete_serato");
  if(it.status==="deleted_in_serato")o.push("delete_rekordbox");
  return o;
}

function renderPlan(){
  const body=$("body");
  if(tab==="log"){body.innerHTML='<pre class="log">'+esc(lastLog||"")+'</pre>';updateSum();return}
  const t=T();
  let h='<table><thead><tr><th class="c1"></th><th>'+t.pl+'</th><th class="num">'+t.cs+'</th><th class="num">'+t.cr+'</th><th class="st">'+t.status+'</th><th class="ac">'+t.action+'</th><th>'+t.note+'</th></tr></thead><tbody>';
  snap.plan.forEach((it,i)=>{
    const opts=actionOptions(it);const cur=picks[it.key]||"skip";
    h+='<tr data-k="'+esc(it.key)+'" class="'+(it.key===sel.key?"sel":"")+'"><td class="c1"><input type="checkbox" data-i="'+i+'" '+(cur!=="skip"?"checked":"")+(opts.length<2?" disabled":"")+'></td>'+
      '<td class="pk">'+esc(it.key)+'</td><td class="num">'+(it.serato?it.serato.count:"–")+'</td><td class="num">'+(it.rekordbox?it.rekordbox.count:"–")+'</td>'+
      '<td class="st"><i class="dot '+it.status+'"></i> '+esc(t.st[it.status]||it.status)+'</td>'+
      '<td class="ac"><select data-i="'+i+'">'+opts.map(a=>'<option value="'+a+'"'+(a===cur?" selected":"")+'>'+esc(t.ac[a])+'</option>').join("")+'</select></td>'+
      '<td title="'+esc(it.note)+'">'+esc(it.note)+'</td></tr>';
  });
  body.innerHTML=h+"</tbody></table>";
  body.querySelectorAll("td.pk").forEach(td=>td.onclick=()=>selectKey(td.parentNode.dataset.k));
  body.querySelectorAll("input[type=checkbox]").forEach(c=>c.onchange=()=>{
    const it=snap.plan[+c.dataset.i];
    picks[it.key]=c.checked?(it.action!=="skip"?it.action:actionOptions(it)[1]):"skip";renderPlan()});
  body.querySelectorAll("select").forEach(s=>s.onchange=()=>{picks[snap.plan[+s.dataset.i].key]=s.value;renderPlan()});
  updateSum();
}
function updateSum(){
  const n=Object.values(picks).filter(v=>v&&v!=="skip").length;
  const diff=snap?snap.plan.filter(p=>p.status!=="same").length:0;
  $("sum").textContent=(snap?snap.plan.length:0)+" · "+(lang==="ko"?"차이 ":"diff ")+diff+" · "+(lang==="ko"?"선택 ":"picked ")+n;
  $("b-run").disabled=!n;
}

async function load(){
  try{
    snap=await api("/api/snapshot");
  }catch(e){$("body").innerHTML='<pre class="log err">'+esc(e.message)+'</pre>';return}
  const t=T();
  $("d-s").className="dot "+(snap.running.serato?"run":"idle");$("d-r").className="dot "+(snap.running.rekordbox?"run":"idle");
  $("t-s").textContent="Serato "+(snap.running.serato?t.run:t.idle);$("t-r").textContent="rekordbox "+(snap.running.rekordbox?t.run:t.idle);
  picks={};
  renderTree("s");renderTree("r");renderPlan();
  if(snap.errors.length)lastLog=snap.errors.join("\\n");
  if(sel.key)await selectKey(sel.key);else{renderGrid("s");renderGrid("r")}
  if(snap.errors.length){tab="log";setTab()}
}
function setTab(){$("tab-plan").classList.toggle("on",tab==="plan");$("tab-log").classList.toggle("on",tab==="log");renderPlan()}

async function sendOne(dir){
  if(!sel.key||!snap)return;
  const it=snap.plan.find(p=>p.key===sel.key);if(!it)return;
  if(dir==="to_rekordbox"&&!it.serato||dir==="to_serato"&&!it.rekordbox)return;
  await runDecisions([{key:it.key,action:dir}]);
}
async function runSelected(){
  await runDecisions(Object.entries(picks).filter(([k,v])=>v&&v!=="skip").map(([key,action])=>({key,action})));
}
async function runDecisions(dec){
  const t=T();
  if(!dec.length){await modal(t.confirm,t.nothing,[{label:t.close,value:1,primary:true}]);return}
  let msg=t.willdo+"\\n\\n"+dec.map(d=>"• "+d.key+"   "+t.ac[d.action]).join("\\n");
  if(dec.some(d=>d.action.startsWith("delete")))msg+="\\n\\n"+t.del;
  const ok=await modal(t.confirm,msg,[{label:t.cancel,value:false},{label:t.ok,value:true,primary:true}]);
  if(!ok)return;
  $("b-run").disabled=true;
  let r;
  try{r=await api("/api/apply",{method:"POST",body:JSON.stringify({decisions:dec})})}
  catch(e){r={ok:false,error:e.message,log:[]}}
  let out=[];
  out.push((r.ok?t.done:t.fail)+(r.error?": "+r.error:""));
  (r.log||[]).forEach(l=>out.push(l));
  if(r.ok&&r.result)(r.result.results||[]).forEach(x=>{
    out.push("• "+x.key+"  "+(t.ac[x.action]||x.action)+"  "+(x.count!=null?x.count+" "+t.tr:"")+"  "+(x.verified?t.verified:t.unverified)+(x.warnings&&x.warnings.length?"\\n    "+x.warnings.join("\\n    "):""))});
  if(r.ok&&r.result&&r.result.backup)out.push("backup: "+r.result.backup);
  lastLog=out.join("\\n");tab="log";setTab();
  await load();tab="log";setTab();
  await modal(r.ok?t.done:t.fail,out.join("\\n"),[{label:t.close,value:1,primary:true}]);
  tab="plan";setTab();
}

async function settings(){
  const c=await api("/api/config");
  const w=document.createElement("div");
  w.innerHTML='<label>'+esc(T().setS)+'</label><input id="i-s"><label>'+esc(T().setR)+'</label><input id="i-r">';
  const p=modal(T().setT,w,[{label:T().cancel,value:false},{label:T().save,value:true,primary:true}]);
  $("i-s").value=c.serato;$("i-r").value=c.rekordbox;
  if(await p){await api("/api/config",{method:"POST",body:JSON.stringify({serato:$("i-s").value.trim(),rekordbox:$("i-r").value.trim()})});sel.key=null;await load()}
}

$("b-ref").onclick=load;$("b-set").onclick=settings;
$("b-r").onclick=()=>sendOne("to_rekordbox");$("b-l").onclick=()=>sendOne("to_serato");
$("b-run").onclick=runSelected;
$("b-all").onclick=()=>{if(!snap)return;snap.plan.forEach(p=>{if(p.status!=="same"&&p.action!=="skip"&&!p.blocked)picks[p.key]=p.action});renderPlan()};
$("b-none").onclick=()=>{picks={};renderPlan()};
$("b-lang").onclick=()=>{lang=lang==="ko"?"en":"ko";try{localStorage.setItem("ps_lang",lang)}catch(e){}setStatic();if(snap){load()}};
$("tab-plan").onclick=()=>{tab="plan";setTab()};$("tab-log").onclick=()=>{tab="log";setTab()};
setStatic();load();
</script>
</body>
</html>
"""

_FLAT = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
_MUS2CAM = {"G#m": "1A", "Abm": "1A", "D#m": "2A", "Ebm": "2A", "A#m": "3A", "Bbm": "3A", "Fm": "4A", "Cm": "5A", "Gm": "6A",
            "Dm": "7A", "Am": "8A", "Em": "9A", "Bm": "10A", "F#m": "11A", "Gbm": "11A", "C#m": "12A", "Dbm": "12A",
            "B": "1B", "F#": "2B", "Gb": "2B", "C#": "3B", "Db": "3B", "G#": "4B", "Ab": "4B", "D#": "5B", "Eb": "5B",
            "A#": "6B", "Bb": "6B", "F": "7B", "C": "8B", "G": "9B", "D": "10B", "A": "11B", "E": "12B"}


def to_camelot(k):
    k = (k or "").strip()
    if not k:
        return ""
    if re.match(r"^\d{1,2}[ABab]$", k):
        return k.upper()
    return _MUS2CAM.get(k, k)


def _ser_row(i, p, m):
    m = m or {}
    try:
        bpm = "%d" % round(float(m.get("bpm"))) if m.get("bpm") else ""
    except ValueError:
        bpm = ""
    return dict(n=i, title=m.get("title") or Path(p).stem, artist=m.get("artist", ""), bpm=bpm,
                key=to_camelot(m.get("key", "")), length=m.get("length", ""), comment=m.get("comment", ""),
                k=pkey(p), path=p, exists=os.path.isfile(p))


def _rb_row(i, r):
    try:
        bpm = "%d" % round(float(r["bpm"])) if r.get("bpm") else ""
    except (ValueError, TypeError):
        bpm = ""
    return dict(n=i, title=r["title"] or Path(r["path"]).stem, artist=r["artist"], bpm=bpm, key=to_camelot(r["key"]),
                length=fmt_len(r["length"]), comment=r["comment"], k=pkey(r["path"]), path=r["path"],
                exists=os.path.isfile(r["path"]))


CONFIG_FILE = DATA_DIR / "config.json"


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_config(c):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(c, ensure_ascii=False, indent=1), "utf-8")


class App:
    def __init__(self, serato=None, rekordbox=None):
        cfg = load_config()
        self.serato_path = serato or cfg.get("serato") or str(default_serato_root())
        self.rb_path = rekordbox or cfg.get("rekordbox") or str(default_rekordbox_db())
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(18)
        self.model = Model(self.serato_path, self.rb_path)
        self.cache = None

    def set_paths(self, serato, rekordbox):
        with self.lock:
            self.serato_path, self.rb_path = serato, rekordbox
            self.model = Model(serato, rekordbox)
            save_config(dict(serato=serato, rekordbox=rekordbox))

    def snapshot(self):
        with self.lock:
            M = self.model
            err = []
            s_nodes, S, r_nodes, R = [], {}, [], {}
            if M.serato.available():
                M.serato._lib = None
                s_nodes, S = M.serato_nodes()
            else:
                err.append("Serato 폴더를 찾을 수 없습니다: %s" % M.serato.sub)
            if M.rb.available():
                try:
                    r_nodes, R = M.rb_nodes()
                except SyncError as e:
                    err.append(str(e))
                finally:
                    M.rb.close()
            else:
                err.append("rekordbox 데이터베이스를 찾을 수 없습니다: %s" % M.rb.path)
            plan = M.plan(S, R) if (S or R) else []
            st = {p["key"]: p["status"] for p in plan}
            for n in s_nodes:
                n["count"] = len(S[n["key"]]["paths"]) if n["key"] in S else None
                n["status"] = st.get(n["key"], "")
            for n in r_nodes:
                n["count"] = len(R[n["key"]]["paths"]) if n["key"] in R else None
                n["status"] = st.get(n["key"], "")
            # folder status: worst child
            for nodes in (s_nodes, r_nodes):
                for n in nodes:
                    if n["kind"] == "folder":
                        kids = [c for c in nodes if c["key"].startswith(n["key"] + "/") and c["kind"] != "folder"]
                        n["status"] = "same" if kids and all(k["status"] == "same" for k in kids) else ("differs" if kids else "")
            return dict(serato=dict(available=M.serato.available(), root=str(M.serato.root), nodes=s_nodes),
                        rekordbox=dict(available=M.rb.available(), path=str(M.rb.path), nodes=r_nodes),
                        plan=plan, errors=err, running=dict(serato=serato_running(), rekordbox=rekordbox_running()),
                        version=VERSION)

    def tracks(self, side, key):
        with self.lock:
            M = self.model
            if side == "serato":
                _, S = M.serato_nodes()
                lib = M.serato.read_library()
                p = S.get(key)
                return [_ser_row(i, x, lib.get(pkey(x))) for i, x in enumerate(p["paths"], 1)] if p else []
            _, R = M.rb_nodes()
            M.rb.close()
            p = R.get(key)
            return [_rb_row(i, r) for i, r in enumerate(p["rows"], 1)] if p else []

    def apply(self, decisions):
        with self.lock:
            logs = []
            try:
                res = self.model.apply(decisions, logs.append)
                return dict(ok=True, result=res, log=logs)
            except SyncError as e:
                return dict(ok=False, error=str(e), log=logs)
            except Exception as e:  # unexpected
                import traceback
                return dict(ok=False, error="예기치 않은 오류: %s" % e, log=logs + [traceback.format_exc()])


def make_handler(app, port):
    allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _ok(self):
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(403, '{"error":"bad host"}')
                return False
            return True

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def _authed(self, qs):
            tok = self.headers.get("X-Token") or (qs.get("t") or [""])[0]
            if not secrets.compare_digest(tok, app.token):
                self._send(401, '{"error":"unauthorized"}')
                return False
            return True

        def do_GET(self):
            if not self._ok():
                return
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if u.path == "/":
                if not self._authed(qs):
                    return
                html = UI_HTML.replace("__TOKEN__", app.token).replace("__VERSION__", VERSION)
                return self._send(200, html, "text/html; charset=utf-8")
            if not self._authed(qs):
                return
            try:
                if u.path == "/api/snapshot":
                    return self._json(app.snapshot())
                if u.path == "/api/tracks":
                    return self._json(app.tracks(qs["side"][0], qs["key"][0]))
                if u.path == "/api/config":
                    return self._json(dict(serato=app.serato_path, rekordbox=app.rb_path))
            except SyncError as e:
                return self._json(dict(error=str(e)), 500)
            except Exception as e:
                return self._json(dict(error="%s" % e), 500)
            self._send(404, '{"error":"not found"}')

        def do_POST(self):
            if not self._ok():
                return
            u = urllib.parse.urlparse(self.path)
            if not self._authed({}):
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json(dict(error="bad json"), 400)
            if u.path == "/api/apply":
                return self._json(app.apply(data.get("decisions", [])))
            if u.path == "/api/config":
                app.set_paths(data["serato"], data["rekordbox"])
                return self._json(dict(ok=True))
            self._send(404, '{"error":"not found"}')

    return H


class _Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_server(app, port=0, open_browser=True):
    srv = _Srv(("127.0.0.1", port), lambda *a, **k: None)
    port = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(app, port)
    url = "http://127.0.0.1:%d/?t=%s" % (port, app.token)
    print("%s %s  →  %s" % (APP_NAME, VERSION, url))
    print("종료: Ctrl+C")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def print_plan(M):
    for it in M.plan():
        print("%-9s %-16s %-6s %-6s %s" % (it["status"], it["action"], (it["serato"] or {}).get("count", "-"),
                                          (it["rekordbox"] or {}).get("count", "-"), it["key"]))


def selftest(app):
    ok = True
    print("%s %s  python %s" % (APP_NAME, VERSION, sys.version.split()[0]))
    M = app.model
    print("Serato   :", M.serato.root, "OK" if M.serato.available() else "NOT FOUND")
    print("rekordbox:", M.rb.path, "OK" if M.rb.available() else "NOT FOUND")
    try:
        _import_pyrekordbox()
        print("pyrekordbox: OK")
    except SyncError as e:
        print("pyrekordbox: MISSING ->", e); ok = False
    if M.serato.available():
        S = M.serato_nodes()[1]
        print("Serato crates:", len(S), "| library tracks:", len(M.serato.read_library()))
    if M.rb.available() and ok:
        try:
            R = M.rb_nodes()[1]; M.rb.close()
            print("rekordbox playlists:", len(R))
        except SyncError as e:
            print("rekordbox open FAILED:", e); ok = False
    print("Serato running:", serato_running(), "| rekordbox running:", rekordbox_running())
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description="Serato DJ Pro <-> rekordbox playlist sync")
    ap.add_argument("--serato", help="path of the _Serato_ folder (default ~/Music/_Serato_)")
    ap.add_argument("--rekordbox", help="path of rekordbox master.db")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--plan", action="store_true", help="print the sync plan and exit")
    ap.add_argument("--selftest", action="store_true", help="check environment and exit")
    ap.add_argument("--install-deps", action="store_true", help="pip install pyrekordbox")
    a = ap.parse_args(argv)
    if a.install_deps:
        sys.exit(subprocess.call([sys.executable, "-m", "pip", "install", "--user", "pyrekordbox"]))
    app = App(a.serato, a.rekordbox)
    if a.selftest:
        sys.exit(0 if selftest(app) else 1)
    if a.plan:
        print_plan(app.model)
        app.model.rb.close()
        return
    run_server(app, a.port, not a.no_browser)


if __name__ == "__main__":
    main()
