"""Shared helpers: config, HTTP with cache + throttling, hashing, name normalization."""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

HOOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../Vault/.hoot
VAULT_DIR = os.path.dirname(HOOT_DIR)
CONFIG_PATH = os.path.join(HOOT_DIR, "config.json")

DEFAULT_CONFIG = {
    "sec_user_agent": "HOOT research tool (set-your-email@example.com)",
    "openalex_mailto": "",
    "opensanctions_api_key": "",
    "notes_folder": "HOOT",
    "sec_max_insider_filings": 80,
    "sec_expand_insiders": True,
    "sec_expand_filings_per_person": 20,
    "gleif_max_children": 50,
    "littlesis_max_pages": 5,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def today():
    return date.today().isoformat()


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_json(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- names

_SUFFIXES = r"\b(incorporated|inc|corporation|corp|company|co|ltd|limited|llc|l\.l\.c|lp|l\.p|plc|ag|sa|nv|bv|gmbh|holdings?|group|the)\b"


def norm_name(s):
    """Exact-ish key: case, punctuation and whitespace folded."""
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def loose_name(s):
    """Looser key used only to *suggest* duplicates, never to auto-merge."""
    s = norm_name(s)
    s = re.sub(_SUFFIXES, " ", s)
    return re.sub(r"\s+", " ", s).strip()


_ORG_HINT = re.compile(
    r"\b(inc|corp|corporation|co|company|llc|lp|l\.p|ltd|plc|fund|funds|trust|holdings?|partners|capital|"
    r"management|advisors?|group|bank|foundation|university|institute|associates|investments?|ventures|"
    r"securities|financial|equity|n\.a|sa|ag|gmbh|limited|partnership|family office|pllc|sarl|gp|"
    r"l\.l\.c|l\.p|s\.a|n\.v|b\.v|a\.g|plc|foundation|endowment|pension|retirement|master fund|offshore|scsp|scs|sca|s\.c\.a|sicav|s\.a\.r\.l|s\.a r\.l|"
    r"aggregator|voteco|strategic investors)\b|\(gp\)", re.I)


def looks_like_org(name):
    return bool(_ORG_HINT.search(name or ""))


_PARTICLES = {"st", "de", "del", "della", "di", "da", "van", "von", "le", "la", "du", "dos", "das", "ten", "ter", "mac"}
_ACRONYMS = {"LLC", "LP", "LLP", "PLC", "USA", "US", "NA", "N.A.", "AG", "SA", "NV", "BV", "II", "III", "IV",
             "IBM", "AT&T", "FMR", "UBS", "BNY", "JP", "HSBC", "BP", "ETF", "REIT", "SPDR", "GE", "UK", "MD", "PHD"}


def _cap_word(w):
    if w.upper() in _ACRONYMS:
        return w.upper()
    if len(w) == 1:
        return w.upper()
    if w.upper().rstrip(".") in {"JR", "SR"}:
        return w[:1].upper() + w[1:].lower()

    def one(p):
        p = p[:1].upper() + p[1:].lower()
        if re.match(r"^(O'|D')\w", p, re.I):                  # O'Connor, D'Angelo
            p = p[:2] + p[2:3].upper() + p[3:]
        elif re.match(r"^Mc\w", p):                           # McDonald
            p = p[:2] + p[2:3].upper() + p[3:]
        return p
    return "-".join(one(p) for p in w.split("-"))


def smart_title(s):
    """Re-case names that arrive mostly ALL CAPS (as EDGAR names do) and drop EDGAR's '/DE/' state tags."""
    if not s:
        return s
    s = re.sub(r"\s*/[A-Za-z]{2,3}/?\s*$", "", s.strip())
    words = s.split()
    caps = [w for w in words if w.isupper() and len(w) > 1]
    if len(caps) * 2 < len([w for w in words if len(w) > 1]):
        return s
    return " ".join(_cap_word(w) if w.isupper() else w for w in words)


def sec_person_name(raw):
    """SEC lists insiders as 'LAST FIRST MIDDLE' -> 'First Middle Last'."""
    parts = (raw or "").replace(",", " ").split()
    if len(parts) < 2:
        return smart_title((raw or "").upper())
    suffix = [p for p in parts if p.upper().rstrip(".") in {"JR", "SR", "II", "III", "IV"}]
    parts = [p for p in parts if p not in suffix]
    if len(parts) < 2:
        return smart_title(" ".join(parts + suffix).upper())
    take = 2 if (len(parts) > 2 and parts[0].lower().rstrip(".") in _PARTICLES) else 1
    last, rest = parts[:take], parts[take:]
    out = rest + last + suffix
    return " ".join(_cap_word(w) if w.isupper() or w.islower() else w for w in out)


def safe_filename(s, maxlen=120):
    s = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", s or "untitled")
    s = re.sub(r"\s+", " ", s).strip().rstrip(".")
    return (s[:maxlen].strip() or "untitled")


# ---------------------------------------------------------------- HTTP

class Http:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cache_dir = os.path.join(HOOT_DIR, "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._last = {}
        self.min_interval = {"data.sec.gov": 0.15, "www.sec.gov": 0.15, "efts.sec.gov": 0.15,
                             "api.openalex.org": 0.12, "littlesis.org": 0.5, "api.gleif.org": 0.25,
                             "api.reporter.nih.gov": 1.0, "api.company-information.service.gov.uk": 0.6, "api.opensanctions.org": 0.5}

    def _headers(self, host):
        if host.endswith("sec.gov"):
            ua = self.cfg.get("sec_user_agent", "")
            if "@" not in ua or "example.com" in ua:
                raise RuntimeError("SEC requires a contact email: set sec_user_agent in .hoot/config.json")
            return {"User-Agent": ua, "Accept-Encoding": "identity"}
        h = {"User-Agent": "HOOT/0.1 (personal research tool)"}
        if host == "api.company-information.service.gov.uk":
            import base64
            key = self.cfg.get("companies_house_api_key") or ""
            h["Authorization"] = "Basic " + base64.b64encode(f"{key}:".encode()).decode()
        if host == "api.opensanctions.org" and self.cfg.get("opensanctions_api_key"):
            h["Authorization"] = "ApiKey " + self.cfg["opensanctions_api_key"]
        return h

    def _cache_path(self, key):
        return os.path.join(self.cache_dir, hashlib.sha1(key.encode()).hexdigest() + ".bin")

    def request(self, url, *, data=None, ttl=86400, extra_headers=None):
        """Returns (bytes, sha256). ttl=None caches forever (immutable filings); ttl=0 bypasses cache."""
        key = url + ("|" + data.decode() if data else "")
        cp = self._cache_path(key)
        if ttl != 0 and os.path.exists(cp):
            if ttl is None or (time.time() - os.path.getmtime(cp)) < ttl:
                with open(cp, "rb") as f:
                    b = f.read()
                return b, sha256_bytes(b)
        host = urllib.parse.urlparse(url).hostname
        headers = self._headers(host)
        if data is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        for attempt in range(4):
            wait = self.min_interval.get(host, 0.2) - (time.time() - self._last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.time()
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as r:
                    b = r.read()
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise
        with open(cp, "wb") as f:
            f.write(b)
        return b, sha256_bytes(b)

    def json(self, url, **kw):
        b, _ = self.request(url, **kw)
        return json.loads(b.decode("utf-8"))

    def post_json(self, url, body, **kw):
        b, _ = self.request(url, data=json.dumps(body, sort_keys=True).encode(), **kw)
        return json.loads(b.decode("utf-8"))


# ---------------------------------------------------------------- person name matching (for sources with no ID)
NICKNAMES = {"chris": "christopher", "mike": "michael", "jim": "james", "jimmy": "james", "bob": "robert", "rob": "robert",
             "bill": "william", "will": "william", "tom": "thomas", "joe": "joseph", "dan": "daniel", "dave": "david",
             "kathy": "kathleen", "kate": "katherine", "margo": "margaret", "peggy": "margaret", "liz": "elizabeth",
             "beth": "elizabeth", "tony": "anthony", "rick": "richard", "dick": "richard", "steve": "steven",
             "ed": "edward", "ted": "edward", "alex": "alexander", "andy": "andrew", "matt": "matthew", "pat": "patricia",
             "sue": "susan", "jen": "jennifer", "jenny": "jennifer", "nick": "nicholas", "sam": "samuel", "ken": "kenneth",
             "jamie": "james", "jack": "john", "hank": "henry", "harry": "henry", "betty": "elizabeth", "betsy": "elizabeth",
             "cathy": "catherine", "chuck": "charles", "charlie": "charles", "ned": "edward", "sandy": "sandra",
             "debbie": "deborah", "patty": "patricia", "trish": "patricia", "meg": "margaret", "maggie": "margaret"}
_SKIP = {"jr", "sr", "ii", "iii", "iv", "dr", "sir", "dame", "mr", "mrs", "ms", "md", "phd", "esq"}


_DEGREES = re.compile(r"(?<![A-Za-z])(ph\.?\s?d|m\.d|j\.d|m\.b\.a|mba|cpa|esq)\.?(?![A-Za-z])", re.I)   # "Robert Berendes, Ph.D."


def name_tokens(name):
    """'Margaret (Margo) H. Georgiadis' -> (first names set, middle initials list, last)"""
    nick = re.findall(r"\((.*?)\)", name or "")
    name = _DEGREES.sub(" ", name or "")
    toks = [t.strip(".,").lower() for t in re.sub(r"\(.*?\)", " ", name).split()]
    toks = [t for t in toks if t and t not in _SKIP]
    if len(toks) < 2:
        return None
    canon = lambda t: {"laurence": "lawrence", "larry": "lawrence", "steven": "stephen", "steve": "stephen"}.get(t, NICKNAMES.get(t, t))
    first = {canon(toks[0]), toks[0]} | {canon(n.lower()) for n in nick}   # 'pat' stays too: Patrick as well as Patricia
    first |= {canon(t) for t in toks[1:-1] if len(t) > 2}      # 'Mary Margaret ...' also answers to Margaret
    middles = [t[0] for t in toks[1:-1]]
    return first, middles, toks[-1]


def _penult(name):
    toks = [t.strip(".,").lower() for t in re.sub(r"\(.*?\)", " ", _DEGREES.sub(" ", name or "")).split()]
    toks = [t for t in toks if t and t not in _SKIP]
    return toks[-2] if len(toks) >= 3 and len(toks[-2]) > 2 else None


def person_name_match(a, b):
    """None (no match), 'first_last', or 'full' (first, last and middle initials all agree)."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return None
    if ta[2] != tb[2] and not (set(ta[2].split("-")) & set(tb[2].split("-"))):   # "Reed-Klages" ~ "Reed"
        return None
    # a word both share just before the surname is part of the surname ("Lauren Santo Domingo" vs
    # "Beatrice De Santo Domingo"), not a shared first name
    pa, pb = [_penult(x) for x in (a, b)]
    if pa and pa == pb and len(ta[0]) > 1 and len(tb[0]) > 1:
        ta, tb = (ta[0] - {pa}, ta[1], ta[2]), (tb[0] - {pb}, tb[1], tb[2])
    if any(len(x) == 1 for x in ta[0] | tb[0]):         # "S Catz": only the first initial is known
        return "initial" if {x[0] for x in ta[0]} & {x[0] for x in tb[0]} else None
    # a shortened form must be a true prefix ('Chris' of 'Christopher'), not just share 3 letters ('Bret' vs 'Brenda')
    if not (ta[0] & tb[0] or any((x.startswith(y) or y.startswith(x)) for x in ta[0] for y in tb[0] if min(len(x), len(y)) >= 3)):
        return None
    if ta[1] and tb[1]:                                     # conflicting middle initials: different people
        return "full" if (set(ta[1]) & set(tb[1]) or ta[1][0] in [x[0] for x in tb[0]] or tb[1][0] in [x[0] for x in ta[0]]) else None
    return "first_last"
