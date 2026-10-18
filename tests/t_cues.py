"""Hot-cue sync (rekordbox -> Serato) on a fixture copy; Serato tag I/O is faked in memory here
(real tag writing is covered by t_cuefile.py)."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(__file__))
import fixture_tools as F
tmp = tempfile.mkdtemp(); os.environ["PLAYLIST_SYNC_DATA"] = tmp + "/data"
A = F.load_app()
A.rekordbox_running = lambda: False; A.serato_running = lambda: False
ser, rb = F.make_env(tmp)
M = A.Model(str(ser), str(rb))
OK, FAIL = [], []
def check(n, c, d=""):
    (OK if c else FAIL).append(n); print("PASS" if c else "FAIL", n, d)
plan = {i["key"]: i for i in M.plan()}
key = [k for k, i in plan.items() if i["status"] == "same"][0]
_, R = M.rb_nodes(); M.rb.close()
row = R[key]["rows"][0]
store = {}                                   # fake Serato tags: path -> {idx: ms}
A.serato_cues = lambda p: dict(store.get(p, {}))
def fake_write(p, cues, bk=None):
    store[p] = dict(cues); return dict(cues)
A.write_serato_cues = fake_write
real_all = A.Rekordbox.all_hot_cues
A.Rekordbox.all_hot_cues = lambda self: {row["content_id"]: {0: 49029, 1: 34345, 3: 38550}}
store[row["path"]] = {1: 1111, 6: 7000}       # Serato already has B (different) and G
it = {i["key"]: i for i in M.plan()}[key]
check("C1 same list + cue diff -> cues_differ / to_serato", it["status"] == "cues_differ" and it["action"] == "to_serato" and it["cue_diff"] == 1, str((it["status"], it.get("cue_diff"))))
res = M.apply([{"key": key, "action": "to_serato"}])
r0 = res["results"][0]
check("C2 apply verified", r0["verified"] and r0["cues"] and r0["cues"][0]["verified"], str(r0))
check("C3 merge: rekordbox slots win, Serato-only slot G kept", store[row["path"]] == {0: 49029, 1: 34345, 3: 38550, 6: 7000}, str(store[row["path"]]))
it = {i["key"]: i for i in M.plan()}[key]
check("C4 after sync -> same", it["status"] == "same", it["status"])
check("C5 kind map", [A.kind_to_idx(k) for k in (0, 1, 2, 3, 4, 5, 9)] == [None, 0, 1, 2, None, 3, 7])
A.Rekordbox.all_hot_cues = real_all
c = A.Rekordbox(str(rb)); h = c.all_hot_cues(); c.close()
check("C6 real fixture DB cue read works", isinstance(h, dict), "%d tracks with hot cues" % len(h))
print("\nPASS", len(OK), "FAIL", len(FAIL)); sys.exit(1 if FAIL else 0)
