"""Identity / Transparency / Integrity enforcement. All rules come from data/governance.json."""
import copy
import hashlib
import json
import re
from datetime import date
from urllib.parse import urlparse

from .store import now_iso


class ValidationError(Exception):
    def __init__(self, errors, status=422):
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))
        self.errors = errors
        self.status = status


def _err(field, message):
    return {"field": field, "message": message}


# ---------------------------------------------------------------- identity

def lei_checksum_ok(lei):
    """ISO 17442 / ISO 7064 MOD 97-10: letters A..Z -> 10..35, whole number mod 97 must equal 1."""
    digits = "".join(str(int(c, 36)) for c in lei)
    return int(digits) % 97 == 1


def identity_errors(identity, gov, field="identity"):
    rules = gov["identity"]
    errs = []
    present = [k for k in rules["accepted_keys"] if (identity or {}).get(k)]
    if not present:
        errs.append(_err(field, "needs at least one canonical identifier: " + ", ".join(rules["accepted_keys"])))
    for k in present:
        v = str(identity[k])
        if not re.fullmatch(rules["patterns"][k], v):
            errs.append(_err(f"{field}.{k}", f"{v!r} is not a valid {k}"))
        elif k == "gleif_lei" and not lei_checksum_ok(v):
            errs.append(_err(f"{field}.{k}", f"{v!r} fails the ISO 17442 check digits"))
    return errs


def node_has_identity(node, gov):
    return not identity_errors(node.get("identity"), gov)


def _identity_index(graph, gov, skip_id=None):
    idx = {}
    for n in graph.get("nodes", []):
        if n.get("id") == skip_id or (n.get("provenance") or {}).get("merged_into"):
            continue                                   # a consolidated duplicate no longer owns its identifiers
        for k in gov["identity"]["accepted_keys"]:
            v = (n.get("identity") or {}).get(k)
            if v:
                idx[(k, str(v))] = n["id"]
    return idx


def _norm_identity(identity):
    out = {}
    for k, v in (identity or {}).items():
        if v in (None, ""):
            continue
        v = str(v).strip()
        if k == "sec_cik" and v.isdigit():
            v = v.zfill(10)
        if k in ("gleif_lei", "canonical_id"):
            v = v.upper()
        if k == "doi":
            v = re.sub(r"^https?://(dx\.)?doi\.org/", "", v, flags=re.I).lower()
        out[k] = v
    return out


def validate_new_node(payload, graph, gov):
    errs = []
    domains = set(graph.get("schema_metadata", {}).get("domains", {}))
    label = (payload.get("label") or "").strip()
    if not label:
        errs.append(_err("label", "required"))
    nid = (payload.get("id") or "").strip().upper() or "ENT_" + re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_")[:50]
    if not re.fullmatch(gov["identity"]["node_id_pattern"], nid):
        errs.append(_err("id", f"{nid!r} must match {gov['identity']['node_id_pattern']}"))
    if any(n["id"] == nid for n in graph.get("nodes", [])):
        errs.append(_err("id", f"{nid} already exists"))
    etype = (payload.get("entity_type") or "").strip().upper()
    if not re.fullmatch(gov["integrity"]["token_pattern"], etype):
        errs.append(_err("entity_type", "required, UPPER_SNAKE_CASE (e.g. CORPORATION)"))
    doms = [d for d in payload.get("domains") or []]
    if not doms:
        errs.append(_err("domains", "pick at least one domain"))
    bad = [d for d in doms if d not in domains]
    if bad:
        errs.append(_err("domains", f"unknown domain(s): {', '.join(bad)}"))
    identity = _norm_identity(payload.get("identity"))
    errs += identity_errors(identity, gov)
    idx = _identity_index(graph, gov)
    for k, v in identity.items():
        if (k, v) in idx:
            errs.append(_err(f"identity.{k}", f"{v} already belongs to {idx[(k, v)]}; one entity, one identifier"))
    if errs:
        raise ValidationError(errs)
    node = {"id": nid, "label": label, "entity_type": etype, "domains": doms, "identity": identity,
            "leadership": [x.strip() for x in payload.get("leadership") or [] if x.strip()],
            "provenance": {"created_at": now_iso(), "created_via": "Add Verified Entity form"}}
    extra = payload.get("provenance")
    if isinstance(extra, dict):
        node["provenance"].update({k: v for k, v in extra.items() if k not in ("created_at",)})
    return node


def validate_identity_addition(node_id, identity, graph, gov):
    """Identifiers may be added to a node, never changed or removed (no silent overrides)."""
    node = next((n for n in graph.get("nodes", []) if n["id"] == node_id), None)
    if not node:
        raise ValidationError([_err("node_id", f"{node_id} not found")], 404)
    identity = _norm_identity(identity)
    errs = []
    if not identity:
        errs.append(_err("identity", "nothing to add"))
    for k, v in identity.items():
        old = (node.get("identity") or {}).get(k)
        if old and str(old) != v:
            errs.append(_err(f"identity.{k}", f"already set to {old}; existing identifiers can't be overwritten"))
    merged = {**(node.get("identity") or {}), **identity}
    errs += [e for e in identity_errors({k: merged[k] for k in identity}, gov) if "needs at least one" not in e["message"]]
    idx = _identity_index(graph, gov, skip_id=node_id)
    for k, v in identity.items():
        if (k, v) in idx:
            errs.append(_err(f"identity.{k}", f"{v} already belongs to {idx[(k, v)]}"))
    if errs:
        raise ValidationError(errs)
    return node, identity


# ---------------------------------------------------------------- edges

def _date(field, v, errs, allow_future=False, required=False):
    if v in (None, ""):
        if required:
            errs.append(_err(field, "required (YYYY-MM-DD)"))
        return None
    try:
        d = date.fromisoformat(str(v))
    except ValueError:
        errs.append(_err(field, "must be a date, YYYY-MM-DD"))
        return None
    if not allow_future and d > date.today():
        errs.append(_err(field, "can't be in the future"))
    return d.isoformat()


def edge_hash(edge):
    body = copy.deepcopy(edge)
    integ = body.get("integrity") or {}
    for k in ("verification_hash", "recorded_at", "superseded_by", "superseded_at", "supersede_reason"):
        integ.pop(k, None)
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


SUPPLEMENT_TRANSPARENCY = ("role_title", "percent_of_class", "evidence_window", "evidence_trail",
                           "source_sha256", "activity_basis")
SUPPLEMENT_INTEGRITY = ("weight_basis",)


def is_superseded(edge):
    return bool((edge.get("integrity") or {}).get("superseded_by"))


def validate_new_edge(payload, graph, gov, edge_id):
    errs = []
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    src, tgt = payload.get("source"), payload.get("target")
    for f, v in (("source", src), ("target", tgt)):
        if v not in nodes:
            errs.append(_err(f, "pick an existing entity"))
        elif gov["integrity"]["require_endpoint_identity"] and not node_has_identity(nodes[v], gov):
            errs.append(_err(f, f"{nodes[v]['label']} has no valid canonical identifier yet; add one before connecting it"))
    if src and src == tgt:
        errs.append(_err("target", "source and target must differ"))

    tok = gov["integrity"]["token_pattern"]
    rtype = (payload.get("relationship_type") or "").strip().upper()
    if not re.fullmatch(tok, rtype):
        errs.append(_err("relationship_type", "required, UPPER_SNAKE_CASE (e.g. BOARD_INTERLOCK)"))
    bridge = (payload.get("human_bridge") or "").strip()
    if len(bridge) > 400:
        errs.append(_err("human_bridge", "keep under 400 characters"))

    # ---- transparency
    t = payload.get("transparency") or {}
    tg = gov["transparency"]
    stype = t.get("verification_source")
    rules = tg["source_types"].get(stype)
    if not rules:
        errs.append(_err("transparency.verification_source", "must be one of: " + ", ".join(tg["source_types"])))
    ref = re.sub(r"\s+", " ", str(t.get("source_reference") or "")).strip()
    if stype in ("DEF 14A", "13F-HR", "NIH RePORTER Grant ID"):
        ref = ref.upper().replace(" ", "")
    if rules:
        if not ref:
            errs.append(_err("transparency.source_reference", f"required: {rules['reference_label']}"))
        elif not re.search(rules["reference_pattern"], ref):
            errs.append(_err("transparency.source_reference", f"doesn't look like a {rules['reference_label']}"))
    url = (t.get("citation_url") or "").strip()
    pu = urlparse(url)
    if not url:
        errs.append(_err("transparency.citation_url", "required: a link to the primary document"))
    elif tg["require_https_url"] and pu.scheme != "https" or not pu.netloc:
        errs.append(_err("transparency.citation_url", "must be a full https:// link"))
    elif rules and rules["url_hosts"] and not any(pu.hostname == h or pu.hostname.endswith("." + h) for h in rules["url_hosts"]):
        errs.append(_err("transparency.citation_url", f"a {stype} citation should link to {' or '.join(rules['url_hosts'])}"))
    doc_date = _date("transparency.document_date", t.get("document_date"), errs, tg["allow_future_document_date"], True)
    ts = _date("transparency.tenure_start", t.get("tenure_start"), errs, True)
    te = _date("transparency.tenure_end", t.get("tenure_end"), errs, True)
    if ts and te and ts > te:
        errs.append(_err("transparency.tenure_end", "ends before it starts"))
    active = t.get("active")
    if not isinstance(active, bool):
        errs.append(_err("transparency.active", "say whether the tie is currently active (true/false)"))
    elif active and te and te < date.today().isoformat():
        errs.append(_err("transparency.active", "marked active but tenure_end is in the past"))

    # ---- integrity
    i = payload.get("integrity") or {}
    ig = gov["integrity"]
    ctype = (i.get("control_type") or "").strip().upper()
    if not re.fullmatch(tok, ctype):
        errs.append(_err("integrity.control_type", "required, UPPER_SNAKE_CASE (e.g. FIDUCIARY_GOVERNANCE)"))
    cclass = i.get("control_class")
    if cclass not in ig["control_classes"]:
        errs.append(_err("integrity.control_class", "must be one of: " + ", ".join(ig["control_classes"])))
    w = i.get("conflict_weight")
    try:
        w = float(w)
        if not (ig["conflict_weight_min"] <= w <= ig["conflict_weight_max"]):
            errs.append(_err("integrity.conflict_weight", f"must be between {ig['conflict_weight_min']:.2f} and {ig['conflict_weight_max']:.2f}"))
        elif round(w, ig["conflict_weight_decimals"]) != w:
            errs.append(_err("integrity.conflict_weight", f"use at most {ig['conflict_weight_decimals']} decimals"))
    except (TypeError, ValueError):
        errs.append(_err("integrity.conflict_weight", "required number, 0.00–1.00"))

    # ---- no silent overrides
    sup = payload.get("supersedes") or {}
    sup_id = (sup.get("edge_id") or "").strip() or None
    existing = [e for e in graph.get("edges", []) if not is_superseded(e) and
                e.get("source") == src and e.get("target") == tgt and e.get("relationship_type") == rtype]
    if existing and sup_id not in [e["edge_id"] for e in existing]:
        errs.append(_err("supersedes", f"{existing[0]['edge_id']} already records {rtype} between these entities. "
                                       "To replace it, supersede it explicitly with a reason; the old edge is kept."))
    if sup_id:
        old = next((e for e in graph.get("edges", []) if e.get("edge_id") == sup_id), None)
        if not old:
            errs.append(_err("supersedes.edge_id", f"{sup_id} not found"))
        elif is_superseded(old):
            errs.append(_err("supersedes.edge_id", f"{sup_id} was already superseded by {old['integrity']['superseded_by']}"))
        if len((sup.get("reason") or "").strip()) < 10:
            errs.append(_err("supersedes.reason", "explain why the old edge is being superseded (10+ characters)"))

    if errs:
        status = 409 if any(e["field"] == "supersedes" for e in errs) and len(errs) == 1 else 422
        raise ValidationError(errs, status)

    edge = {
        "edge_id": edge_id, "source": src, "target": tgt, "relationship_type": rtype,
        "human_bridge": bridge,
        "transparency": {"verification_source": stype, "source_reference": ref, "citation_url": url,
                         "document_date": doc_date, "date_verified": date.today().isoformat(), "active": active},
        "integrity": {"control_type": ctype, "control_class": cclass, "conflict_weight": round(w, 2)},
    }
    if ts:
        edge["transparency"]["tenure_start"] = ts
    if te:
        edge["transparency"]["tenure_end"] = te
    if sup_id:
        edge["integrity"]["supersedes"] = sup_id
    # optional supporting evidence (e.g. from the HOOT bridge); whitelisted and covered by the hash
    sup_data = payload.get("supplement") or {}
    for k in SUPPLEMENT_TRANSPARENCY:
        if sup_data.get(k) not in (None, "", [], {}):
            edge["transparency"][k] = sup_data[k]
    for k in SUPPLEMENT_INTEGRITY:
        if sup_data.get(k) not in (None, "", [], {}):
            edge["integrity"][k] = sup_data[k]
    if isinstance(sup_data.get("provenance"), dict):
        edge["provenance"] = sup_data["provenance"]
    edge["integrity"]["verification_hash"] = edge_hash(edge)
    edge["integrity"]["recorded_at"] = now_iso()
    return edge, sup_id, (sup.get("reason") or "").strip()


# ---------------------------------------------------------------- whole-graph audit

def audit_graph(graph, gov):
    """Report every node/edge that falls short of the mandate. Nothing is changed or hidden."""
    issues = []
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    doms = set(graph.get("schema_metadata", {}).get("domains", {}))
    seen = {}
    for n in graph.get("nodes", []):
        if (n.get("provenance") or {}).get("merged_into"):
            continue
        for e in identity_errors(n.get("identity"), gov):
            other = [k for k in (n.get("identity") or {}) if k not in gov["identity"]["accepted_keys"] and k != "jurisdiction"]
            hint = f" (has only {', '.join(other)}, which aren't canonical identifiers)" if other and "needs" in e["message"] else ""
            issues.append({"kind": "node", "id": n["id"], "core": "Identity", "severity": "error",
                           "message": e["message"] + hint})
        for k in gov["identity"]["accepted_keys"]:
            v = (n.get("identity") or {}).get(k)
            if v:
                if (k, v) in seen:
                    issues.append({"kind": "node", "id": n["id"], "core": "Identity", "severity": "error",
                                   "message": f"{k} {v} is also used by {seen[(k, v)]}"})
                seen[(k, v)] = n["id"]
        for d in n.get("domains") or []:
            if d not in doms:
                issues.append({"kind": "node", "id": n["id"], "core": "Identity", "severity": "warning",
                               "message": f"unknown domain {d}"})
    stypes = gov["transparency"]["source_types"]
    for e in graph.get("edges", []):
        eid = e.get("edge_id")
        if is_superseded(e):                           # history, kept on purpose: report as resolved, not open
            issues.append({"kind": "edge", "id": eid, "core": "Integrity", "severity": "resolved",
                           "message": f"replaced by {e['integrity']['superseded_by']}: {e['integrity'].get('supersede_reason', '')}"[:300]})
            continue
        for f in ("source", "target"):
            if e.get(f) not in nodes:
                issues.append({"kind": "edge", "id": eid, "core": "Identity", "severity": "error",
                               "message": f"{f} {e.get(f)} is not a node in the graph"})
        t = e.get("transparency") or {}
        vs = t.get("verification_source") or ""
        if vs not in stypes:
            cited = [s for s in stypes if s.lower() in vs.lower()]
            msg = (f"cites {', '.join(cited)} in free text but not as a structured citation" if cited
                   else f"source {vs!r} is not one of the accepted primary-source types")
            issues.append({"kind": "edge", "id": eid, "core": "Transparency", "severity": "warning" if cited else "error",
                           "message": msg})
        missing = [k for k in ("citation_url", "document_date", "source_reference") if not t.get(k)]
        if missing:
            issues.append({"kind": "edge", "id": eid, "core": "Transparency", "severity": "warning",
                           "message": "missing " + ", ".join(missing) + " (can't be independently re-checked)"})
        i = e.get("integrity") or {}
        w = i.get("conflict_weight")
        if not isinstance(w, (int, float)) or not 0 <= w <= 1:
            issues.append({"kind": "edge", "id": eid, "core": "Integrity", "severity": "error",
                           "message": "conflict_weight missing or outside 0.00–1.00"})
        if i.get("verification_hash") and i["verification_hash"] != edge_hash(e):
            issues.append({"kind": "edge", "id": eid, "core": "Integrity", "severity": "error",
                           "message": "edited after it was recorded: verification_hash no longer matches"})
        if not i.get("verification_hash"):
            issues.append({"kind": "edge", "id": eid, "core": "Integrity", "severity": "info",
                           "message": "no verification_hash (recorded before the form existed)"})
    return issues
