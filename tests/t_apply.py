import os, sys, tempfile, shutil, struct, time, unicodedata
sys.path.insert(0, os.path.dirname(__file__))
from fixture_tools import *
A = load_app()
OK = []; FAIL = []
def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name); print(("PASS " if cond else "FAIL ") + name, extra if not cond else "")

def fresh():
    tmp = tempfile.mkdtemp(); os.environ["PLAYLIST_SYNC_DATA"] = tmp + "/data"; A.DATA_DIR = A.Path(tmp + "/data")
    ser, rbdb = make_env(tmp)
    return tmp, A.Model(ser, rbdb)

def sigs(paths): return [A.pkey(p) for p in paths]

# pick Serato-library tracks that rekordbox does not know yet, with metadata
tmp, M = fresh()
lib = M.serato.read_library(); idx = M.rb.content_index(); M.rb.close()
cands = [m for k, m in lib.items() if k not in idx and m["artist"] and m["comment"] and m["bpm"] and m["path"].startswith("/Users/pandolm1/Music")][:3]
print("new-track candidates:", [(c["title"], c["artist"], c["bpm"], c["key"]) for c in cands])
for c in cands: make_stub(c["path"])
known = [v.FolderPath for v in list(idx.values())[:50] if os.path.isfile(v.FolderPath) or True][:3]

# ---------------- A: Serato -> rekordbox (new nested playlist, duplicates, new tracks)
want = [known[0], cands[0]["path"], known[0], known[1], cands[1]["path"], known[2]]
M.serato.write_crate("TESTF%%Sub%%New1", want)
plan = {i["key"]: i for i in M.plan()}
it = plan["TESTF/Sub/New1"]
check("A1 plan new_serato", it["status"] == "new_serato" and it["action"] == "to_rekordbox", it)
res = M.apply([{"key": "TESTF/Sub/New1", "action": "to_rekordbox"}])
check("A2 verified", all(r["verified"] for r in res["results"]), res)
M2 = A.Model(M.serato.root, M.rb.path)
_, R = M2.rb_nodes(); M2.rb.close()
check("A3 order+duplicates exact", sigs(R["TESTF/Sub/New1"]["paths"]) == sigs(want))
rows = {A.pkey(r["path"]): r for r in R["TESTF/Sub/New1"]["rows"]}
c0 = rows[A.pkey(cands[0]["path"])]
check("A4 new track meta (artist/title/comment/bpm)", c0["artist"] == cands[0]["artist"] and c0["comment"] == cands[0]["comment"] and abs(c0["bpm"] - float(cands[0]["bpm"])) < 0.01 and c0["title"] == (cands[0]["title"] or A.Path(cands[0]["path"]).stem), c0)
p2 = {i["key"]: i for i in M2.plan()}
check("A5 now 'same'", p2["TESTF/Sub/New1"]["status"] == "same", p2["TESTF/Sub/New1"])
check("A6 backup created", os.path.isdir(res["backup"]) and os.path.isfile(os.path.join(res["backup"], "rekordbox", "master.db")))

# ---------------- C: change Serato order -> rekordbox follows ; change rekordbox -> serato follows
new_order = list(reversed(want))
M2.serato.write_crate("TESTF%%Sub%%New1", new_order)
time.sleep(0.01)
it = {i["key"]: i for i in M2.plan()}["TESTF/Sub/New1"]
check("C1 serato_changed detected", it["status"] == "serato_changed" and it["action"] == "to_rekordbox", it["status"])
res = M2.apply([{"key": it["key"], "action": it["action"]}])
M3 = A.Model(M.serato.root, M.rb.path); _, R = M3.rb_nodes(); M3.rb.close()
check("C2 reorder applied exactly", sigs(R["TESTF/Sub/New1"]["paths"]) == sigs(new_order))

# rekordbox-side change through pyrekordbox
db = M3.rb.open()
pl = db.get_playlist(Name="New1").all()[0]
songs = sorted(pl.Songs, key=lambda s: s.TrackNo)
db.remove_from_playlist(pl, songs[0])  # drop first
M3.rb.close()
it = {i["key"]: i for i in M3.plan()}["TESTF/Sub/New1"]
check("C3 rekordbox_changed detected", it["status"] == "rekordbox_changed" and it["action"] == "to_serato", it["status"])
res = M3.apply([{"key": it["key"], "action": it["action"]}])
cr = M3.serato.read_crates()["TESTF%%Sub%%New1"]["paths"]
check("C4 serato crate follows", sigs(cr) == sigs(new_order[1:]), (len(cr), len(new_order)))
check("C5 verified", all(r["verified"] for r in res["results"]))

# ---------------- B: rekordbox -> serato (new)
db = M3.rb.open()
f = db.create_playlist_folder("RBF"); g = db.create_playlist("Gamma", parent=f)
ids = [v for v in list(idx.values())[10:14]]
for c in [ids[0], ids[1], ids[0], ids[2], ids[3]]: db.add_to_playlist(g, c)
db.commit(); M3.rb.close()
it = {i["key"]: i for i in M3.plan()}["RBF/Gamma"]
check("B1 plan new_rekordbox", it["status"] == "new_rekordbox" and it["action"] == "to_serato")
res = M3.apply([{"key": "RBF/Gamma", "action": "to_serato"}])
cr = M3.serato.read_crates()["RBF%%Gamma"]["paths"]
check("B2 crate exact w/ duplicates", sigs(cr) == sigs([ids[0].FolderPath, ids[1].FolderPath, ids[0].FolderPath, ids[2].FolderPath, ids[3].FolderPath]))
np_ = (M3.serato.root / "neworder.pref").read_bytes().decode("utf-16-be")
check("B3 neworder.pref updated, still UTF-16BE", "[crate]RBF%%Gamma" in np_ and np_.rstrip().endswith("[end record]"))
check("B4 crate header kept columns", b"ovct" in (M3.serato.root / "Subcrates" / "RBF%%Gamma.crate").read_bytes())

# ---------------- D: conflict (both changed)
base = {i["key"]: i for i in M3.plan()}["RBF/Gamma"]
check("D0 gamma same after sync", base["status"] == "same")
M3.serato.write_crate("RBF%%Gamma", cr[:2])
db = M3.rb.open(); g = db.get_playlist(Name="Gamma").all()[0]; db.add_to_playlist(g, ids[3]); db.commit(); M3.rb.close()
it = {i["key"]: i for i in M3.plan()}["RBF/Gamma"]
check("D1 conflict detected", it["status"] == "both_changed", it["status"])

# ---------------- E: deletion is never automatic
os.remove(str(M3.serato.sub / "RBF%%Gamma.crate"))
it = {i["key"]: i for i in M3.plan()}["RBF/Gamma"]
check("E1 deletion detected but skipped by default", it["status"] == "deleted_in_serato" and it["action"] == "skip", it["status"])

# ---------------- G: delete_serato removes crate file and neworder entry
M3.serato.write_crate("RBF%%Delta", cr[:1]); 
np0 = (M3.serato.root / "neworder.pref").read_bytes().decode("utf-16-be")
check("G0 Delta in neworder", "[crate]RBF%%Delta" in np0)
M3.serato.remove_crate_to("RBF%%Delta", str(M3.serato.root.parent / "removed"))
np1 = (M3.serato.root / "neworder.pref").read_bytes().decode("utf-16-be")
check("G1 crate moved + neworder entry removed", "RBF%%Delta" not in np1 and not (M3.serato.sub / "RBF%%Delta.crate").exists())

# ---------------- F: running guards
A.rekordbox_running = lambda: True
try:
    M3.apply([{"key": "TESTF/Sub/New1", "action": "to_rekordbox"}]); check("F1 rekordbox-running guard", False)
except A.SyncError as e:
    check("F1 rekordbox-running guard", "rekordbox" in str(e))
A.rekordbox_running = lambda: False
A.serato_running = lambda: True
try:
    M3.apply([{"key": "TESTF/Sub/New1", "action": "to_serato"}]); check("F2 serato-running guard", False)
except A.SyncError as e:
    check("F2 serato-running guard", "Serato" in str(e))
print("\nPASS", len(OK), "FAIL", len(FAIL), FAIL)
sys.exit(1 if FAIL else 0)
