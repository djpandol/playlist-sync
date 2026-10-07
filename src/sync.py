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
