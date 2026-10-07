import os, sys, tempfile, json
sys.path.insert(0, os.path.dirname(__file__))
from fixture_tools import *
tmp = tempfile.mkdtemp(); os.environ["PLAYLIST_SYNC_DATA"] = tmp + "/data"
A = load_app()
ser, rbdb = make_env(tmp)
M = A.Model(ser, rbdb)
for it in M.plan():
    print("%-55s %-20s %-13s S=%s R=%s %s" % (it["key"], it["status"], it["action"], (it["serato"] or {}).get("count"), (it["rekordbox"] or {}).get("count"), it["diff"] or ""))
nodes, pl = M.serato_nodes()
print([ (n["key"],n["kind"]) for n in nodes])
