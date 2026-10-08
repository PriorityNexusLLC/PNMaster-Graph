"""Back up the vault and Priority Nexus data to the USB drive (the "backup" folder in data/paths.json).

Skips quietly when the drive isn't plugged in. Mirrors:
  <backup>/Obsidian Vault          the whole vault (notes, HOOT database, reports)
  <backup>/PriorityNexus data      data/ (graph, audit log, reports) without the per-save graph backups
and copies the audit anchors there too: a fingerprint record kept off this computer, so the log's history can be
proven even if this computer were changed.

    python -I src/maintenance/backup_to_usb.py
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import audit, paths  # noqa: E402


def mirror(src, dst, extra=()):
    # /FFT: FAT32 (most USB sticks) stores times to 2 seconds; without it every file would look changed
    r = subprocess.run(["robocopy", src, dst, "/MIR", "/COPY:DT", "/FFT", "/R:1", "/W:2", "/XJ", "/NP", "/NFL", "/NDL", *extra],
                       capture_output=True, text=True)
    return r.returncode < 8


def main():
    b = paths.BACKUP
    if not b:
        print("backup: no backup folder set (data/paths.json 'backup'); skipped")
        return 0
    drive = os.path.splitdrive(b)[0] + "\\"
    if not os.path.exists(drive):
        print(f"backup: {drive} not plugged in; skipped")
        return 0
    os.makedirs(b, exist_ok=True)
    ok1 = mirror(paths.VAULT, os.path.join(b, "Obsidian Vault"))
    ok2 = mirror(os.path.join(ROOT, "data"), os.path.join(b, "PriorityNexus data"), ["/XD", os.path.join(ROOT, "data", "backups")])
    a = audit.anchor()                                  # today's fingerprint, also written to the vault's anchor note
    shutil.copy2(audit.ANCHOR_PATH, os.path.join(b, "Audit anchors (USB copy).md"))
    audit.log_event("usb_backup", folder=b, vault_ok=ok1, data_ok=ok2, anchored_tip=a["tip"])
    print(f"backup: vault {'ok' if ok1 else 'FAILED'} · data {'ok' if ok2 else 'FAILED'} · anchor {a['tip'][:16]}… -> {b}")
    return 0 if ok1 and ok2 else 1


if __name__ == "__main__":
    sys.exit(main())
