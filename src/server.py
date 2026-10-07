# ----------------------------------------------------------------------------
# SERVER : tiny local web UI (stdlib only) + CLI
# ----------------------------------------------------------------------------
import threading, secrets, argparse, http.server, socketserver, urllib.parse, webbrowser, io, contextlib

UI_HTML = """__UI_HTML__"""

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
