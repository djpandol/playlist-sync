# ----------------------------------------------------------------------------
# CUES : hot cues A-H between rekordbox (master.db djmdCue) and Serato (file tag "Serato Markers2")
#   rekordbox djmdCue.Kind: 0 = memory cue, hot cue A-C = 1-3, D-H = 5-9 (4 is used by loops)
#   Serato Markers2 CUE entry: index 0-7 = A-H, position in milliseconds
#   Sources: pyrekordbox db6 format docs; github.com/takayamaki/rbx PR #7;
#            "Reversing Serato's GEOB tags" (J. Holthuis); github.com/Holzhaus/serato-tags
# ----------------------------------------------------------------------------
import base64

LETTERS = "ABCDEFGH"
_KIND2IDX = {1: 0, 2: 1, 3: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7}
CUE_EXTS = {".mp3", ".wav", ".aif", ".aiff"}      # formats whose Serato tags live in ID3
DEFAULT_RGB = b"\xcc\x00\x00"                    # red, as documented for Serato CUE colour bytes
_GEOB_DESC = "Serato Markers2"
_MIN_GEOB = 470                                  # size Serato pads the frame to (observed)


def kind_to_idx(kind):
    try:
        return _KIND2IDX.get(int(kind))
    except (TypeError, ValueError):
        return None


def fmt_cues(cues):
    """{idx: ms} -> 'A 0:49.0  B 0:34.3'"""
    out = []
    for i in sorted(cues):
        ms = cues[i]
        d = (int(ms) + 50) // 100                     # tenths of a second, rounded half up
        out.append("%s %d:%02d.%d" % (LETTERS[i], d // 600, (d % 600) // 10, d % 10))
    return "  ".join(out)


def _mutagen():
    try:
        import mutagen
        from mutagen import id3
        return mutagen, id3
    except ImportError:
        raise SyncError("mutagen 이 없습니다.  터미널에서:  python3 -m pip install mutagen")


def _open_tags(path, create=False):
    """Return (file_object, id3_tags) for mp3 / wav / aiff, or (None, None)."""
    mutagen, id3 = _mutagen()
    ext = os.path.splitext(path)[1].lower()
    if ext not in CUE_EXTS or not os.path.isfile(path):
        return None, None
    if ext == ".mp3":
        from mutagen.mp3 import MP3
        f = MP3(path)
    elif ext == ".wav":
        from mutagen.wave import WAVE
        f = WAVE(path)
    else:
        from mutagen.aiff import AIFF
        f = AIFF(path)
    if f.tags is None and create:
        f.add_tags()
    return f, f.tags


# ---- Markers2 payload <-> entries
def _decode_geob(data):
    if data[:2] == b"\x01\x01":
        data = data[2:]
    data = data.split(b"\x00", 1)[0].replace(b"\n", b"").replace(b"\r", b"")
    for cut in (0, 1, 2, 3):                       # tolerate odd lengths / missing padding
        s = data[:len(data) - cut] if cut else data
        try:
            return base64.b64decode(s + b"=" * (-len(s) % 4))
        except Exception:
            continue
    return b""


def parse_markers2(data):
    """GEOB data -> list of (name, bytes) entries"""
    p = _decode_geob(data)
    if p[:2] != b"\x01\x01":
        return []
    i, out = 2, []
    while i < len(p):
        j = p.find(b"\x00", i)
        if j <= i:
            break
        name = p[i:j].decode("latin1")
        if j + 5 > len(p):
            break
        ln = struct.unpack(">I", p[j + 1:j + 5])[0]
        out.append((name, p[j + 5:j + 5 + ln]))
        i = j + 5 + ln
    return out


def build_markers2(entries):
    payload = b"\x01\x01" + b"".join(n.encode("latin1") + b"\x00" + struct.pack(">I", len(d)) + d for n, d in entries) + b"\x00"
    b64 = base64.b64encode(payload)
    b64 = b"\n".join(b64[i:i + 72] for i in range(0, len(b64), 72))
    data = b"\x01\x01" + b64
    return data + b"\x00" * max(1, _MIN_GEOB - len(data))


def cue_entry(idx, ms, rgb=DEFAULT_RGB, name=""):
    return b"\x00" + bytes([idx]) + struct.pack(">I", int(ms)) + b"\x00" + rgb + b"\x00\x00" + name.encode("utf-8") + b"\x00"


def parse_cue_entry(d):
    if len(d) < 13:
        return None
    return dict(idx=d[1], ms=struct.unpack(">I", d[2:6])[0], rgb=d[7:10], name=d[12:].split(b"\x00", 1)[0].decode("utf-8", "replace"))


# ---- public: read / write Serato hot cues of one file
_SER_CACHE = {}


def serato_cues(path):
    """{idx: ms} of hot cues in the file's Serato tag, None if format unsupported / unreadable"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime, st.st_size)
    if key in _SER_CACHE:
        return _SER_CACHE[key]
    res = None
    try:
        f, tags = _open_tags(path)
        if f is not None:
            res = {}
            if tags is not None:
                for fr in tags.getall("GEOB"):
                    if fr.desc == _GEOB_DESC:
                        for n, d in parse_markers2(fr.data):
                            if n == "CUE":
                                c = parse_cue_entry(d)
                                if c and c["idx"] < 8:
                                    res[c["idx"]] = c["ms"]
    except SyncError:
        raise
    except Exception:
        res = None
    _SER_CACHE[key] = res
    return res


def write_serato_cues(path, cues, backup_dir=None):
    """Replace the file's Serato hot cues with `cues` ({idx: ms}). Other Markers2 entries
    (track colour, BPM lock, loops, flips) are kept; colours/names of slots that already exist are kept.
    Returns the hot cues read back from disk."""
    mutagen, id3 = _mutagen()
    f, tags = _open_tags(path, create=True)
    if f is None:
        raise SyncError("Serato 핫큐를 쓸 수 없는 형식: " + path)
    if backup_dir:
        Path(backup_dir).mkdir(parents=True, exist_ok=True)
        dst = Path(backup_dir) / Path(path).name
        n = 1
        while dst.exists():
            dst = Path(backup_dir) / ("%s.%d%s" % (Path(path).stem, n, Path(path).suffix)); n += 1
        shutil.copy2(path, dst)
    old = []
    for fr in tags.getall("GEOB"):
        if fr.desc == _GEOB_DESC:
            old = parse_markers2(fr.data)
    prev = {}
    kept = []
    for n, d in old:
        if n == "CUE":
            c = parse_cue_entry(d)
            if c:
                prev[c["idx"]] = c
        else:
            kept.append((n, d))
    new = []
    for idx in sorted(cues):
        p = prev.get(idx)
        new.append(("CUE", cue_entry(idx, cues[idx], p["rgb"] if p else DEFAULT_RGB, p["name"] if p else "")))
    # Serato order: COLOR first, then CUEs, then the rest
    color = [e for e in kept if e[0] == "COLOR"]
    rest = [e for e in kept if e[0] != "COLOR"]
    data = build_markers2(color + new + rest)
    tags.delall("GEOB:" + _GEOB_DESC)
    # Older "Serato Markers_" tag also carries cues; serato-tools removes it so Serato shows the Markers2 values
    tags.delall("GEOB:Serato Markers_")
    tags.add(id3.GEOB(encoding=0, mime="application/octet-stream", filename="", desc=_GEOB_DESC, data=data))
    ver = getattr(tags, "version", (2, 4, 0))
    if isinstance(f.tags, id3.ID3) and ver[1] == 3:
        f.save(v2_version=3)          # keep the file's existing ID3 version
    else:
        f.save()
    _SER_CACHE.clear()
    got = serato_cues(path)
    return got
