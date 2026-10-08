import copy
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from engine import pathfinder, store, validator  # noqa: E402

# The investigator's original graph reduced to what official records confirm (src/maintenance/build_public_fixture.py)
_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_fixture_graph.json")
GRAPH = store.load_graph(_FIXTURE)
needs_fixture = unittest.skipUnless(GRAPH, "public_fixture_graph.json missing")
GOV = store.load_governance()


def good_edge(**over):
    e = {
        "source": "ENT_LOCKHEED", "target": "ENT_CHEVRON", "relationship_type": "BOARD_INTERLOCK",
        "human_bridge": "Debra L. Reed-Klages",
        "transparency": {"verification_source": "DEF 14A", "source_reference": "0001193125-26-012345",
                         "citation_url": "https://www.sec.gov/Archives/edgar/data/936468/x.htm",
                         "document_date": "2026-03-13", "active": True},
        "integrity": {"control_type": "FIDUCIARY_GOVERNANCE", "control_class": "hard", "conflict_weight": 0.88},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(e.get(k), dict):
            e[k] = {**e[k], **v}
        else:
            e[k] = v
    return e


def fields(ex):
    return {x["field"] for x in ex.exception.errors}


@needs_fixture
class Identity(unittest.TestCase):
    def test_lei_checksum(self):
        self.assertTrue(validator.lei_checksum_ok("DPRBOZP0K5RM2YE8UU08"))   # Lockheed Martin Corp
        self.assertFalse(validator.lei_checksum_ok("DPRBOZP0K5RM2YE8UU09"))

    def test_node_without_canonical_id_rejected(self):
        with self.assertRaises(validator.ValidationError) as ex:
            validator.validate_new_node({"label": "Acme", "entity_type": "CORPORATION", "domains": ["DOM_A"],
                                         "identity": {"ticker": "ACME"}}, GRAPH, GOV)
        self.assertIn("identity", fields(ex))

    def test_duplicate_identifier_rejected(self):
        with self.assertRaises(validator.ValidationError) as ex:
            validator.validate_new_node({"label": "Lockheed Again", "entity_type": "CORPORATION",
                                         "domains": ["DOM_A"], "identity": {"sec_cik": "936468"}}, GRAPH, GOV)
        self.assertIn("identity.sec_cik", fields(ex))

    def test_identity_cannot_be_overwritten(self):
        with self.assertRaises(validator.ValidationError):
            validator.validate_identity_addition("ENT_LOCKHEED", {"sec_cik": "0000000001"}, GRAPH, GOV)

    def test_identity_can_be_added(self):
        node, ids = validator.validate_identity_addition("ENT_LOCKHEED", {"gleif_lei": "DPRBOZP0K5RM2YE8UU08"}, GRAPH, GOV)
        self.assertEqual(ids, {"gleif_lei": "DPRBOZP0K5RM2YE8UU08"})

    def test_endpoint_without_identity_blocked(self):
        g = copy.deepcopy(GRAPH)                     # a real record with its official ID taken away
        next(n for n in g["nodes"] if n["id"] == "ENT_AIRBUS")["identity"] = {}
        with self.assertRaises(validator.ValidationError) as ex:
            validator.validate_new_edge(good_edge(target="ENT_AIRBUS"), g, GOV, "EDGE_999")
        self.assertIn("target", fields(ex))


@needs_fixture
class Transparency(unittest.TestCase):
    def test_valid_edge_accepted_and_hashed(self):
        e, sup, _ = validator.validate_new_edge(good_edge(), GRAPH, GOV, "EDGE_999")
        self.assertEqual(e["integrity"]["verification_hash"], validator.edge_hash(e))
        self.assertEqual(e["transparency"]["date_verified"], date.today().isoformat())

    def test_source_type_must_be_allowed(self):
        with self.assertRaises(validator.ValidationError) as ex:
            validator.validate_new_edge(good_edge(transparency={"verification_source": "Wikipedia"}), GRAPH, GOV, "E")
        self.assertIn("transparency.verification_source", fields(ex))

    def test_reference_format_enforced(self):
        with self.assertRaises(validator.ValidationError) as ex:
            validator.validate_new_edge(good_edge(transparency={"source_reference": "page 14"}), GRAPH, GOV, "E")
        self.assertIn("transparency.source_reference", fields(ex))

    def test_sec_citation_must_link_to_sec(self):
        with self.assertRaises(validator.ValidationError) as ex:
            validator.validate_new_edge(good_edge(transparency={"citation_url": "https://example.com/x"}), GRAPH, GOV, "E")
        self.assertIn("transparency.citation_url", fields(ex))

    def test_future_document_date_rejected(self):
        fut = (date.today() + timedelta(days=3)).isoformat()
        with self.assertRaises(validator.ValidationError) as ex:
            validator.validate_new_edge(good_edge(transparency={"document_date": fut}), GRAPH, GOV, "E")
        self.assertIn("transparency.document_date", fields(ex))

    def test_statute_and_nih_patterns(self):
        stat = good_edge(source="ENT_US_TREASURY", target="ENT_CFIUS", relationship_type="STATUTORY_OVERSIGHT",
                         transparency={"verification_source": "Statute", "source_reference": "50 U.S.C. § 4565",
                                       "citation_url": "https://www.law.cornell.edu/uscode/text/50/4565"})
        validator.validate_new_edge(stat, GRAPH, GOV, "E")
        nih = good_edge(source="ENT_HARVARD_CHAN", target="ENT_PFIZER", relationship_type="GRANT_FUNDING",
                        transparency={"verification_source": "NIH RePORTER Grant ID", "source_reference": "r01ai177514",
                                      "citation_url": "https://reporter.nih.gov/project-details/11111111"},
                        integrity={"control_class": "soft"})
        e, _, _ = validator.validate_new_edge(nih, GRAPH, GOV, "E")
        self.assertEqual(e["transparency"]["source_reference"], "R01AI177514")


@needs_fixture
class Integrity(unittest.TestCase):
    def test_weight_bounds_and_precision(self):
        for w in (1.2, -0.1, 0.123, "x"):
            with self.assertRaises(validator.ValidationError) as ex:
                validator.validate_new_edge(good_edge(integrity={"conflict_weight": w}), GRAPH, GOV, "E")
            self.assertIn("integrity.conflict_weight", fields(ex))

    def test_no_silent_override(self):
        g = copy.deepcopy(GRAPH)
        e, _, _ = validator.validate_new_edge(good_edge(), g, GOV, "EDGE_900")
        g["edges"].append(e)
        with self.assertRaises(validator.ValidationError) as ex:
            validator.validate_new_edge(good_edge(), g, GOV, "EDGE_901")
        self.assertEqual(ex.exception.status, 409)
        e2, sup, reason = validator.validate_new_edge(
            good_edge(supersedes={"edge_id": "EDGE_900", "reason": "2026 proxy shows new committee role"}), g, GOV, "EDGE_901")
        self.assertEqual(sup, "EDGE_900")

    def test_tamper_detected(self):
        g = copy.deepcopy(GRAPH)
        e, _, _ = validator.validate_new_edge(good_edge(), g, GOV, "EDGE_900")
        e["integrity"]["conflict_weight"] = 0.1
        g["edges"].append(e)
        msgs = [i["message"] for i in validator.audit_graph(g, GOV) if i["id"] == "EDGE_900"]
        self.assertTrue(any("verification_hash" in m for m in msgs))

    def test_public_example_meets_all_three_cores(self):
        self.assertEqual([i for i in validator.audit_graph(GRAPH, GOV) if i["severity"] == "error"], [])

    def test_audit_flags_missing_identity_and_source(self):
        g = copy.deepcopy(GRAPH)
        next(n for n in g["nodes"] if n["id"] == "ENT_ARAMCO")["identity"] = {}
        g["edges"][0]["transparency"]["citation_url"] = ""
        issues = validator.audit_graph(g, GOV)
        self.assertIn("ENT_ARAMCO", {i["id"] for i in issues if i["core"] == "Identity"})
        self.assertTrue(any(i["id"] == g["edges"][0]["edge_id"] for i in issues))


@needs_fixture
class Paths(unittest.TestCase):
    def test_entity_path(self):
        r = pathfinder.find_paths(GRAPH, from_ids=["ENT_CHEVRON"], to_ids=["ENT_PALO_ALTO"])
        self.assertEqual(r["paths"][0]["nodes"], ["ENT_CHEVRON", "ENT_LOCKHEED", "ENT_PALO_ALTO"])
        self.assertTrue(r["paths"][0]["crossings"])

    def test_inactive_edges_excluded_by_default(self):
        g = copy.deepcopy(GRAPH)
        next(e for e in g["edges"] if e["edge_id"] == "EDGE_4667")["transparency"]["active"] = False
        r = pathfinder.find_paths(g, from_ids=["ENT_PFIZER"], to_ids=["ENT_OPENAI"])
        self.assertFalse(r["paths"])
        r = pathfinder.find_paths(g, from_ids=["ENT_PFIZER"], to_ids=["ENT_OPENAI"], include_inactive=True)
        self.assertEqual(len(r["paths"][0]["nodes"]), 2)

    def test_domain_to_domain(self):
        r = pathfinder.find_paths(GRAPH, from_domains=["DOM_E"], to_domains=["DOM_A"], include_inactive=True)
        self.assertIn("spanning_nodes", r)

    def test_superseded_edges_not_traversed(self):
        g = copy.deepcopy(GRAPH)
        for e in g["edges"]:
            if e["edge_id"] == "EDGE_4456":
                e["integrity"]["superseded_by"] = "EDGE_X"
        r = pathfinder.find_paths(g, from_ids=["ENT_CHEVRON"], to_ids=["ENT_PALO_ALTO"])
        self.assertFalse(r["paths"])


class AuditChain(unittest.TestCase):
    """The audit log is tamper-evident: edits, deletions and rewrites of history are all detected."""

    def setUp(self):
        import tempfile
        from engine import audit
        self.audit, self.dir = audit, tempfile.mkdtemp()
        self.saved = {k: getattr(audit, k) for k in ("LOG_PATH", "LOCK_PATH", "ANCHOR_PATH", "GRAPH_PATH")}
        audit.LOG_PATH = os.path.join(self.dir, "log.jsonl")
        audit.LOCK_PATH = os.path.join(self.dir, "log.lock")
        audit.ANCHOR_PATH = os.path.join(self.dir, "anchors.md")
        audit.GRAPH_PATH = os.path.join(self.dir, "none.json")
        audit._tip_cache.update(size=None)
        audit._verify_cache.clear()
        with open(audit.LOG_PATH, "w", encoding="utf-8") as f:
            f.write('{"ts": "t0", "event": "legacy"}\n')
        for i in range(4):
            audit.log_event("test", i=i)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.audit, k, v)
        self.audit._tip_cache.update(size=None)
        self.audit._verify_cache.clear()

    def rewrite(self, fn):
        with open(self.audit.LOG_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
        with open(self.audit.LOG_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(fn(lines)) + "\n")
        self.audit._tip_cache.update(size=None)

    def test_intact(self):
        r = self.audit.verify()
        self.assertTrue(r["ok"])
        self.assertEqual((r["entries"], r["sealed_legacy_lines"], r["chained"]), (6, 1, 5))

    def test_edit_detected(self):
        self.rewrite(lambda L: L[:3] + [L[3].replace('"i": 1', '"i": 7')] + L[4:])
        self.assertIn("content changed", " ".join(b["problem"] for b in self.audit.verify()["breaks"]))

    def test_deletion_detected(self):
        self.rewrite(lambda L: L[:3] + L[4:])
        self.assertIn("does not point", " ".join(b["problem"] for b in self.audit.verify()["breaks"]))

    def test_sealed_lines_protected(self):
        self.rewrite(lambda L: [L[0].replace("legacy", "changed")] + L[1:])
        self.assertFalse(self.audit.verify()["ok"])

    def test_anchor_catches_full_rewrite(self):
        self.audit.anchor()
        self.rewrite(lambda L: L[:1])                   # throw away the chain, then rebuild a valid-looking one
        for i in range(5):
            self.audit.log_event("test", i=i * 10)
        r = self.audit.verify()
        self.assertFalse(r["ok"])
        self.assertTrue(r["anchors"]["missing"])


class NoLostWrites(unittest.TestCase):
    def test_save_refuses_when_file_changed_since_load(self):
        import json
        import tempfile
        import time
        p = os.path.join(tempfile.mkdtemp(), "g.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"nodes": [], "edges": []}, f)
        a = store.load_graph(p)
        b = store.load_graph(p)
        time.sleep(0.02)
        store.save_graph(b, p)                       # another program saves first
        with self.assertRaises(store.GraphChangedError):
            store.save_graph(a, p)                   # the stale copy may not overwrite it
        store.save_graph(b, p)                       # the fresh copy still can


class Balance(unittest.TestCase):
    def graph(self):
        g = {"schema_metadata": {"domains": {"DOM_A": "A"}}, "nodes": [], "edges": []}
        add_n = lambda i, t="PERSON", d=("DOM_A",): g["nodes"].append({"id": i, "label": i, "entity_type": t, "domains": list(d)})
        n = [0]

        def add_e(s, t, rt, first=None, last="2026-01-01"):
            n[0] += 1
            g["edges"].append({"edge_id": f"E{n[0]}", "source": s, "target": t, "relationship_type": rt,
                               "transparency": {"active": True, "document_date": last,
                                                "evidence_window": {"first_filing": first or last, "last_filing": last}},
                               "integrity": {}})
        for o in ("CO", "PARENT", "OTHER"):
            add_n(o, "ORGANIZATION")
        for p in ("P1", "P2", "P3", "P4"):
            add_n(p)
        add_e("PARENT", "CO", "TEN_PERCENT_OWNER_OF")
        add_e("P1", "CO", "DIRECTOR_OF", first="2010-01-01")
        add_e("P1", "CO", "OFFICER_OF")
        add_e("P2", "CO", "DIRECTOR_OF")
        add_e("P2", "PARENT", "OFFICER_OF")
        add_e("P3", "CO", "DIRECTOR_OF")
        add_e("P4", "OTHER", "DIRECTOR_OF")
        add_e("P4", "CO", "DIRECTOR_OF")
        return g

    def test_flags(self):
        from engine import balance
        r = balance.compute(self.graph())
        ind = {f["org"]: f for f in r["independence"]["flags"]}
        self.assertEqual(ind["CO"]["tied"], 2)                           # P1 officer, P2 at the major owner
        self.assertEqual(r["tenure"]["flags"][0]["person"], "P1")         # 16 years > 9
        cut = {f["id"]: f["cut_off"] for f in balance.single_points(*balance._live(self.graph()))["flags"]}
        self.assertNotIn("P4", cut)                                       # removing P4 cuts off one entity: below the floor
        self.assertGreaterEqual(min(balance.gates.BALANCE_STANDARDS["independence_min_share"][0], 1), 0)

    def test_superseded_and_historical_ignored(self):
        from engine import balance
        g = self.graph()
        for e in g["edges"]:
            e["transparency"]["active"] = False
        self.assertEqual(balance.compute(g)["independence"]["flags"], [])


class ReportReview(unittest.TestCase):
    """The second pass stops risky wording and enforces the right of reply."""

    def test_wording(self):
        from engine import review
        md = ("Signals for review, not findings of wrongdoing. There is no evidence of fraud.\n"
              "The board secretly colluded in order to enrich themselves, which is illegal.\n"
              "Reach her at jane@example.com or 123 Maple Street.\n"
              "| Acme | 2026-01-02 email | \"We deny any illegal conduct.\" |\n")
        hits = {(w["status"], w["word"].lower().rstrip(".")) for w in review.check_wording(md)}
        self.assertIn(("block", "illegal"), hits)
        for w in ("secretly", "colluded", "in order to", "jane@example.com", "123 maple street"):
            self.assertIn(w, {h[1] for h in hits})
        self.assertNotIn("fraud", {h[1] for h in hits})                  # negated
        self.assertEqual(sum(1 for h in hits if h[1] == "illegal"), 1)     # the quoted reply is theirs

    def test_false_claims_hold(self):
        from engine import review
        clean = "Filings report Jane Doe as director of Acme. Signals for review, not findings of wrongdoing.\n"
        self.assertFalse(review.check_fca(clean)["hold"])
        md = clean + "Acme also received federal contracts and billed the agency for the same work twice.\n"
        self.assertTrue(review.check_fca(md)["hold"])
        self.assertIn("false claims", {w["word"].lower() for w in review.check_wording("They made false claims.")})
        cleared = md + "\nLegal consult: 2026-11-02, Example Whistleblower Law, cleared for publication\n"
        r = review.check_fca(cleared)
        self.assertFalse(r["hold"])
        self.assertEqual(r["cleared"]["date"], "2026-11-02")

    def test_enforcement_section_is_attributed_not_linted(self):
        from engine import review
        md = ("## 4. Findings\n### 4.5 Public enforcement records (as published by the agency)\n"
              "- U.S. Department of Justice press release: \"Acme to Pay $5 Million to Resolve False Claims Act Allegations\". "
              "Described by the Department as: criminal case (as described by DOJ).\n## 5. Limits\nFine text.\n")
        self.assertEqual(review.check_wording(md), [])
        self.assertFalse(review.check_fca(md)["hold"])

    def test_enforcement_name_patterns(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "ingest"))
        import enforcement
        pat, generic = enforcement.org_pattern("Target Corp")
        self.assertTrue(generic)
        self.assertIsNone(pat.search("shoppers at a target store"))
        self.assertIsNotNone(pat.search("Target Corporation agreed"))
        pat, _ = enforcement.org_pattern("Eli Lilly & Co")
        self.assertIsNotNone(pat.search("Eli Lilly and Company Agrees to Pay"))
        p = enforcement.person_pattern("Kelly Ayotte")
        self.assertIsNotNone(p.search("Senator Kelly A. Ayotte said"))
        self.assertIsNone(p.search("Kelly Smith and John Ayotte"))

    def test_reply_window(self):
        from engine import review
        md = ("## 6. Right of reply\n\n| Organization | Contacted (date, how) | Response |\n|---|---|---|\n"
              f"| A | {date.today().isoformat()} email | |\n| B | 2020-01-01 email | |\n| C | | |\n| D | 2026-01-01 | Declined |\n\n## 7. x\n")
        r = review.check_reply(md, ["A", "B", "C", "D"])
        self.assertEqual((r["waiting"], r["not_contacted"], sorted(r["done"])), (["A"], ["C"], ["B", "D"]))


if __name__ == "__main__":
    unittest.main()
