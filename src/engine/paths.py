"""Where the Obsidian vault lives: ONE setting for every script.

    data/paths.json    {"vault": "%USERPROFILE%\\Documents\\Obsidian Vault"}

If the file is missing, the original OneDrive location is used. `relocate()` maps a path recorded under an
earlier vault location (e.g. a report's working copy) to the current one, so moving the vault breaks nothing.
`python -I src/engine/paths.py` prints the vault folder (used by the .cmd files)."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG = os.path.join(ROOT, "data", "paths.json")
DEFAULT_VAULT = os.path.join(os.path.expanduser("~"), "OneDrive", "Documents", "Obsidian Vault")
KNOWN_OLD = [DEFAULT_VAULT]


def _config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _vault():
    v = _config().get("vault")
    return os.path.normpath(os.path.expandvars(os.path.expanduser(v))) if v else DEFAULT_VAULT


VAULT = _vault()
KNOWN_OLD += [os.path.normpath(p) for p in _config().get("previous", [])]
BACKUP = _config().get("backup")          # e.g. a folder on the USB drive; used by backup_to_usb
HOOT_DIR = os.path.join(VAULT, ".hoot")
if not os.path.isdir(HOOT_DIR) and os.path.isdir(os.path.join(ROOT, "hoot")):   # the public copy keeps HOOT in ./hoot
    HOOT_DIR = os.path.join(ROOT, "hoot")
HOOT_DB = os.path.join(HOOT_DIR, "graph.db")
HOOT_NOTES = os.path.join(VAULT, "HOOT")
REPORTS = os.path.join(HOOT_NOTES, "Reports")


def relocate(p):
    """A path saved under an earlier vault location -> the same file in the current vault."""
    if not p or os.path.exists(p):
        return p
    for old in KNOWN_OLD + [_v for _v in (os.environ.get("PN_OLD_VAULT"),) if _v]:
        if os.path.normcase(p).startswith(os.path.normcase(old)):
            return os.path.join(VAULT, p[len(old):].lstrip("\\/"))
    return p


if __name__ == "__main__":
    print(VAULT)
