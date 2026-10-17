# PyInstaller spec — builds "Playlist Sync.app" (Apple Silicon)
import re, pathlib
from PyInstaller.utils.hooks import collect_all, collect_submodules
ROOT = pathlib.Path(SPECPATH).resolve().parents[1]
VERSION = re.search(r'^VERSION = "([^"]+)"', (ROOT / "src" / "engine.py").read_text(), re.M).group(1)
datas, binaries, hidden = [(str(ROOT / "THIRD_PARTY_NOTICES.txt"), "."), (str(ROOT / "LICENSE"), ".")], [], []
for pkg in ("pyrekordbox", "sqlcipher3", "webview", "sqlalchemy"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
hidden += collect_submodules("sqlalchemy.dialects.sqlite")

a = Analysis([str(ROOT / "playlist_sync.py")], pathex=[str(ROOT)], binaries=binaries, datas=datas,
             hiddenimports=hidden, excludes=["tkinter", "matplotlib", "PIL"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Playlist Sync", console=False,
          target_arch="arm64", argv_emulation=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Playlist Sync", upx=False)
app = BUNDLE(coll, name="Playlist Sync.app", icon=None, bundle_identifier="com.djpandol.playlistsync",
             version=VERSION,
             info_plist={
                 "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
                 "CFBundleDisplayName": "Playlist Sync", "LSMinimumSystemVersion": "12.0",
                 "NSHighResolutionCapable": True,
                 "NSHumanReadableCopyright": "Free, non-commercial (MIT). Unofficial — not affiliated with Serato or AlphaTheta.",
                 "NSDocumentsFolderUsageDescription": "Playlist Sync checks that the music files in your playlists exist.",
                 "NSDesktopFolderUsageDescription": "Playlist Sync checks that the music files in your playlists exist.",
                 "NSDownloadsFolderUsageDescription": "Playlist Sync checks that the music files in your playlists exist.",
                 "NSRemovableVolumesUsageDescription": "Playlist Sync reads music files and libraries on external drives.",
                 "NSNetworkVolumesUsageDescription": "Playlist Sync reads music files on network drives.",
             })
