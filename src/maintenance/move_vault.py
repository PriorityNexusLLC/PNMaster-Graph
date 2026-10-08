"""Move the Obsidian vault (notes + HOOT) to a new folder, safely.

  1. refuses to run while Obsidian or a HOOT / Priority Nexus job is running
  2. copies everything (robocopy), then verifies: same number of files, same total size, and identical
     fingerprints (SHA-256) for HOOT's database, its settings and the audit anchors
  3. only if every check passes: records the new location in data/paths.json (every script follows it),
     and RENAMES the old folder to "Obsidian Vault (OLD - moved)". Nothing is deleted; delete the old folder
     yourself once you've opened the new vault in Obsidian and looked around.

    python -I src/maintenance/move_vault.py "%USERPROFILE%\\Documents\\Obsidian Vault" [--backup "D:\\Priority Nexus backup"]
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import audit, paths  # noqa: E402


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def inventory(folder):
    n = size = 0
    for d, _, files in os.walk(folder):
        for fn in files:
            n += 1
            size += os.path.getsize(os.path.join(d, fn))
    return n, size


def busy():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'Obsidian*' -or "
                          "($_.Name -like 'python*' -and $_.CommandLine -match 'hoot|ingest|maintenance\\\\backup') } | "
                          "Select-Object -ExpandProperty Name"], capture_output=True, text=True).stdout.split()
    return [x for x in out if x]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("destination")
    ap.add_argument("--backup", help="also record a backup folder (e.g. on the USB drive) for backup_to_usb")
    a = ap.parse_args()
    src, dst = paths.VAULT, os.path.normpath(a.destination)
    if os.path.normcase(src) == os.path.normcase(dst):
        sys.exit("The vault is already there.")
    if os.path.exists(dst) and os.listdir(dst):
        sys.exit(f"{dst} already has files in it; choose an empty or new folder.")
    b = busy()
    if b:
        sys.exit(f"Please close these first: {', '.join(sorted(set(b)))} (Obsidian, or a HOOT / Priority Nexus job).")
    print(f"copying {src}\n     -> {dst}\n(OneDrive may download cloud-only files first; this can take a while)")
    r = subprocess.run(["robocopy", src, dst, "/E", "/COPY:DAT", "/DCOPY:T", "/R:2", "/W:3", "/XJ", "/NP", "/NFL", "/NDL"],
                       capture_output=True, text=True)
    if r.returncode >= 8:
        sys.exit("copy failed (robocopy code %d):\n%s" % (r.returncode, r.stdout[-2000:]))
    (n1, s1), (n2, s2) = inventory(src), inventory(dst)
    checks = [os.path.join(".hoot", "graph.db"), os.path.join(".hoot", "config.json"),
              os.path.join("HOOT", "Reports", "Audit anchors.md")]
    bad = [c for c in checks if os.path.exists(os.path.join(src, c)) and sha(os.path.join(src, c)) != sha(os.path.join(dst, c))]
    print(f"files: {n1} -> {n2} · bytes: {s1:,} -> {s2:,} · fingerprints: {'all match' if not bad else 'MISMATCH ' + ', '.join(bad)}")
    if n1 != n2 or s1 != s2 or bad:
        sys.exit("Verification failed: nothing was switched. The original vault is untouched and still in use.")
    cfg = paths._config()
    cfg["vault"] = dst
    cfg["previous"] = sorted(set(cfg.get("previous", []) + [src]))
    if a.backup:
        cfg["backup"] = a.backup
    with open(paths.CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    old = src + " (OLD - moved)"
    try:
        os.rename(src, old)
        renamed = True
    except OSError as ex:
        renamed = False
        print(f"note: couldn't rename the old folder ({ex}); it is no longer used, rename or delete it yourself later")
    audit.log_event("vault_moved", source=src, destination=dst, files=n2, bytes=s2, verified=True,
                    old_folder=old if renamed else src)
    print(f"\nDone. Every script now uses {dst}.\nIn Obsidian: 'Open another vault' -> 'Open folder as vault' -> choose that folder."
          f"\nThe old copy is at {old if renamed else src}; delete it once you're happy.")


if __name__ == "__main__":
    main()
