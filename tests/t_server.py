"""Server/API test on a fixture copy: auth, host check, snapshot, tracks, apply."""
import sys, os, json, tempfile, threading, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(__file__))
import fixture_tools as F
tmp = tempfile.mkdtemp()
os.environ["PLAYLIST_SYNC_DATA"] = tmp + "/data"
A = F.load_app()
ser, rb = F.make_env(tmp)
app = A.App(str(ser), str(rb))
A.rekordbox_running = lambda: False; A.serato_running = lambda: False
srv = A._Srv(("127.0.0.1", 0), lambda *a, **k: None); port = srv.server_address[1]
srv.RequestHandlerClass = A.make_handler(app, port)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port
OK, FAIL = [], []
def check(n, c, d=""):
    (OK if c else FAIL).append(n); print("PASS" if c else "FAIL", n, d)
def get(path, tok=True, host=None):
    h = {"X-Token": app.token} if tok else {}
    if host: h["Host"] = host
    try:
        r = urllib.request.urlopen(urllib.request.Request(base + path, headers=h)); return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
check("S1 no token -> 401", get("/api/snapshot", tok=False)[0] == 401)
check("S2 bad Host -> 403", get("/api/snapshot", host="evil.example")[0] == 403)
st, b = get("/api/snapshot"); sn = json.loads(b)
check("S3 snapshot ok", st == 200 and sn["serato"]["nodes"] and sn["rekordbox"]["nodes"] and sn["plan"])
st, b = get("/?t=" + app.token); check("S4 html served with token", st == 200 and app.token.encode() in b and b"Playlist Sync" in b)
k = [p for p in sn["plan"] if p["status"] == "new_rekordbox"][0]["key"]
import urllib.parse
st, b = get("/api/tracks?side=rekordbox&key=" + urllib.parse.quote(k)); rows = json.loads(b)
check("S5 tracks", st == 200 and len(rows) > 0 and "title" in rows[0], str(len(rows)))
req = urllib.request.Request(base + "/api/apply", data=json.dumps({"decisions": [{"key": k, "action": "to_serato"}]}).encode(),
                             headers={"X-Token": app.token, "Content-Type": "application/json"})
res = json.loads(urllib.request.urlopen(req).read())
check("S6 apply ok + verified", res["ok"] and res["result"]["results"][0]["verified"], str(res.get("error")))
st, b = get("/api/snapshot"); sn2 = json.loads(b)
check("S7 now in sync", [p for p in sn2["plan"] if p["key"] == k][0]["status"] == "same")
print("\nPASS", len(OK), "FAIL", len(FAIL)); sys.exit(1 if FAIL else 0)
