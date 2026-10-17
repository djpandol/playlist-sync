"""Write THIRD_PARTY_NOTICES.txt: licenses of every component shipped inside the Mac app."""
import subprocess, sys, pathlib
here = pathlib.Path(__file__).resolve().parent
out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "THIRD_PARTY_NOTICES.txt")
parts = ["THIRD-PARTY SOFTWARE NOTICES — Playlist Sync\n",
         "Playlist Sync bundles the components below. Each is used under its own license, reproduced here.\n"]
for name, f, note in [
    ("SQLCipher (Community Edition) — statically linked inside sqlcipher3-wheels", "SQLCipher.txt", ""),
    ("OpenSSL — statically linked inside sqlcipher3-wheels", "OpenSSL.txt", ""),
    ("PyInstaller bootloader", "PyInstaller.txt", "Apps built with PyInstaller may be distributed under any license (bootloader exception, see below).\n"),
]:
    parts.append("\n" + "=" * 78 + "\n" + name + "\n" + "=" * 78 + "\n" + note + (here / "licenses" / f).read_text("utf-8"))
py_lic = pathlib.Path(sys.base_prefix) / "lib" / ("python%d.%d" % sys.version_info[:2]) / "LICENSE.txt"
if py_lic.is_file():
    parts.append("\n" + "=" * 78 + "\nPython %s (PSF License)\n" % sys.version.split()[0] + "=" * 78 + "\n" + py_lic.read_text("utf-8", "replace"))
r = subprocess.run([sys.executable, "-m", "piplicenses", "--with-license-file", "--no-license-path", "--format=plain-vertical",
                    "--with-urls", "--ignore-packages", "pip-licenses", "prettytable", "wcwidth", "pyinstaller", "pyinstaller-hooks-contrib",
                    "altgraph", "macholib", "packaging", "setuptools", "pip", "tomli"], capture_output=True, text=True)
if r.returncode != 0:
    sys.exit("pip-licenses failed: " + r.stderr)
parts.append("\n" + "=" * 78 + "\nPython packages\n" + "=" * 78 + "\n" + r.stdout)
out.write_text("".join(parts), "utf-8")
print("wrote", out, out.stat().st_size, "bytes")
