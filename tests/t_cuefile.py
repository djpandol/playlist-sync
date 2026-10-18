import sys, os, shutil, hashlib
sys.path.insert(0,"/home/claude/playlist-sync/tests")
import fixture_tools as F
A=F.load_app()
D=os.environ.get("CUE_DIR","/tmp/claude-0/cue")
OK=[];FAIL=[]
def check(n,c,d=""):
    (OK if c else FAIL).append(n); print("PASS" if c else "FAIL", n, d)
want={0:49029,1:34345,2:76158,3:38550}
for f in ("a.mp3","b.wav","c.aiff"):
    p=os.path.join(D,f)
    before=A.serato_cues(p)
    got=A.write_serato_cues(p,want,D+"/bk")
    check(f+" write+readback", got==want, str(got))
    check(f+" backup exists", os.path.exists(os.path.join(D,"bk",f)))
    # audio stream unchanged: compare decoded audio md5 via ffmpeg
    import subprocess
    h=lambda x: subprocess.run(["ffmpeg","-loglevel","error","-i",x,"-f","md5","-"],capture_output=True,text=True).stdout.strip()
    check(f+" audio unchanged", h(p)==h(os.path.join(D,"bk",f)), h(p))
# independent parser (serato-tools) on mp3 + aiff
from serato_tools.track_cues_v2 import TrackCuesV2
for f in ("a.mp3","c.aiff"):
    t=TrackCuesV2(os.path.join(D,f))
    cues={e.index:e.position for e in t.entries if type(e).__name__=="CueEntry"}
    check(f+" serato-tools reads same cues", cues==want, str(cues))
# merge keeps other slots + id3 version kept
from mutagen.id3 import ID3
check("a.mp3 id3 v2.3 kept", ID3(os.path.join(D,"a.mp3")).version[:2]==(2,3), str(ID3(os.path.join(D,"a.mp3")).version))
got=A.write_serato_cues(os.path.join(D,"a.mp3"),{**want,5:1000},None)
check("rewrite idempotent+slot F", got=={**want,5:1000}, str(got))
check("fmt", A.fmt_cues({0:49029,3:38550})=="A 0:49.0  D 0:38.6", A.fmt_cues({0:49029,3:38550}))
print("PASS",len(OK),"FAIL",len(FAIL)); sys.exit(1 if FAIL else 0)
