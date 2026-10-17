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
import re as _re
ver = _re.search(r"^VERSION = \"([^\"]+)\"", parts[0], _re.M).group(1)
out = head % ver + "\n".join(parts)
(r / "playlist_sync.py").write_text(out, "utf-8")
print("built", len(out.splitlines()), "lines")
