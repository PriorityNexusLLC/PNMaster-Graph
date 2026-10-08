# CLAUDE.md — Priority Nexus Topological & Epistemic Forensics Engine

## Foundational Mandate
Every schema, ingestion script, UI view, and verification algorithm in this repository is governed by three non-negotiable cores:
1. **Identity:** Every entity (corporation, agency, person, or scientific study) MUST resolve to an immutable canonical identifier (`sec_cik`, `gleif_lei`, `doi`, or `canonical_id`). Never create loose string-matched nodes.
2. **Transparency:** Every directed edge in `data/priority_nexus_graph.json` MUST carry a verifiable statutory or primary-source citation (`DEF 14A`, `13F-HR`, `Form 990`, `NIH RePORTER Grant ID`, or `Statute`) inside its `transparency` object.
3. **Integrity:** No silent overrides or unverified edges. Automated conflict-detection and pathfinding routines must treat boundary crossings as measurable, logged events (`conflict_weight` 0.00–1.00).

## Repository Architecture
- `data/priority_nexus_graph.json`: Master multi-domain property graph covering Domains A–G (Defense, Retail, Finance/Sovereign, Tech/Hardware, Life Sciences, Energy/Chemistry, Media/Telecom).
- `src/ingest/`: Scripts for querying public APIs (SEC EDGAR `data.sec.gov`, OpenAlex `api.openalex.org`, GLEIF API) and resolving entities into the master JSON graph.
- `src/engine/`: Multi-hop graph traversal, shortest-path interlock finder, and epistemic funding-trail auditor.
- `src/ui/`: Interactive force-directed and hierarchical tree visualization dashboard.

## Coding Standards for Claude Code
- Do NOT introduce external proprietary SaaS dependencies or cloud databases that compromise local sovereignty. Prefer local SQLite/KùzuDB/JSON + clean Python or TypeScript/D3.js.
- Do NOT expose or fabricate proprietary Priority Nexus mathematical threshold equations; keep governance gate interfaces modular.