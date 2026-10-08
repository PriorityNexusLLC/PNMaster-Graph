# PNMaster-Graph

**Priority Nexus™ Master Graph**: evidence-first mapping of institutional power from official public records.
By Priority Nexus LLC · Systems Assurance Forensic Investigation & Design.

Who sits on which boards, who owns significant stakes in whom, who lobbies for whom and who used to work where:
assembled from official filings into one verifiable map, measured against published governance standards, and
reported without accusation. The aim is balance, transparency and integrity, and helping institutions improve.
Start with the [concept paper](docs/concept-paper.md).

## Principles
- **Identity:** every organization and person is tied to an official registry ID; nothing is joined by name alone.
- **Transparency:** every connection links to the primary public record it comes from.
- **Integrity:** nothing is silently changed; records are fingerprinted and the event log is hash-chained.

## What's here
| Folder | Contents |
|---|---|
| `src/engine` | validator (nine gates), path finder, balance measures, pattern signals, report drafting and two-pass review, hash-chained audit log |
| `src/ingest` | connectors: SEC, IRS 990, U.S. Senate and House rosters, lobbying disclosures (LDA), foreign agents (FARA), campaign finance (FEC), DOJ releases, HHS-OIG exclusions, helper Inbox |
| `src/ui` | local web app (no external dependencies) |
| `hoot` | HOOT, the evidence collector and identity resolver (Obsidian-based) |
| `data` | public rule tables only: governance gates, sector tables, committee jurisdictions, the rulebook of laws and standards |
| `docs` | concept paper, publishing-safety guide |

No investigation data, reports, keys or personal information are included.

## Run it
Python 3.11+ (standard library only). `run.cmd` starts the app at http://127.0.0.1:8765. `update_all.cmd` runs every
collector. HOOT needs a contact email for SEC requests in `hoot/config.json` (not included).

## License
**Apache License 2.0** (see LICENSE and NOTICE): use it freely, with credit to Priority Nexus LLC.
Priority Nexus™ is a trademark of Priority Nexus LLC.

## Part of
The **Priority Nexus Deterministic Governance & Circuit-Breaker Architecture**, the concept this project implements:
https://github.com/PriorityNexusLLC/Deterministic-Governance-Circuit-Breaker-Architecture

## Team & contact
* **Architecture & Systems Lead:** Josie Anderson
* **Contact:** theaistherapist@gmail.com

Signals produced by this software are for review, not findings of wrongdoing.
