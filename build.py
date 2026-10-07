#!/usr/bin/env python3
"""Concatenate src parts + UI into the single distributable file playlist_sync.py"""
import pathlib
r = pathlib.Path(__file__).parent
head = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Playlist Sync %s -- two-way playlist sync between Serato DJ Pro and rekordbox.
Non-commercial, free for everyone (MIT).  Run:  python3 playlist_sync.py
"""
'''
parts = [(r / "src" / n).read_text("utf-8") for n in ("engine.py", "sync.py")]
srv = (r / "src" / "server.py")
if srv.exists():
    ui = (r / "ui" / "index.html").read_text("utf-8") if (r / "ui" / "index.html").exists() else ""
    parts.append(srv.read_text("utf-8").replace("__UI_HTML__", ui.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')))
out = head % "0.1.0" + "\n".join(parts)
(r / "playlist_sync.py").write_text(out, "utf-8")
print("built", len(out.splitlines()), "lines")
