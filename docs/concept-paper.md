# Priority Nexus™: Evidence-First Mapping of Institutional Power

**A concept paper on verifiable, balanced, non-accusatory governance forensics**

Priority Nexus LLC · Systems Assurance Forensic Investigation & Design
Josie Anderson, Architecture & Systems Lead · theaistherapist@gmail.com
Version 0.1 · October 2026

> Published openly as a public, dated record of this method, so that it stays free for anyone to use and can't
> be patented by others.

---

## Abstract

Power in modern institutions rarely sits in one place. It travels through people: the same individuals serve on
corporate boards, fund boards, nonprofit boards, government committees and advisory councils, and move between
them over a career. Each of these roles is usually disclosed somewhere in a public filing, but the filings are
scattered across agencies, formats and decades. Priority Nexus is a method and an open toolset for assembling
those disclosures into one verifiable map, measuring it against published governance standards, and reporting
what it shows **without accusation**. It is built on three non-negotiable principles: **Identity**,
**Transparency** and **Integrity**. Its purpose is not only to show where institutions fall short, but to help
them improve.

## 1. The problem

- **Scattered records.** Board seats sit in securities filings, nonprofit boards in tax returns, lobbying in
  legislative disclosures, foreign representation in a separate registry, enforcement outcomes in press releases.
  No single record shows the whole picture.
- **Names are unreliable.** People change names, use nicknames, initials and maiden names; organizations share
  names. Mapping by name alone produces false connections, and false connections harm real people.
- **Accusation travels faster than evidence.** Public discussion of influence too often relies on insinuation.
  That erodes trust in institutions and in those who scrutinize them alike.
- **Flags without help.** Watchdog work usually ends at a finding. The organizations involved rarely receive a
  clear picture of the rule, the standard, and what good practice would look like.

## 2. Principles

1. **Identity.** Every organization and person is tied to an official registry identifier (for example an SEC
   filer number, a Legal Entity Identifier, a tax ID, or a legislative biographical ID). Nothing is joined by name
   alone: a match needs two independent facts, and two different official identifiers are never merged without a
   human decision.
2. **Transparency.** Every connection carries a link to the primary public record it comes from. Secondary
   sources are leads, never evidence. A finding with no source link is not reported.
3. **Integrity.** Nothing is silently changed. Corrections are new records that point to the old ones; every
   record and every save is fingerprinted, and the event log is hash-chained so that altering history is
   detectable, with periodic fingerprints kept outside the system.

These are enforced as nine gates, three per principle:

| Identity | Transparency | Integrity |
|---|---|---|
| Official ID | Primary source | No silent change |
| Two facts | Receipt (link and reference) | Fingerprint |
| One ID, one entity | Recency (current vs historical) | Boundary crossings measured, not assumed |

## 3. How it works

1. **Official sources.** Securities filings (insider and ownership reports, proxy statements, private offering
   notices), nonprofit tax returns, legislative rosters, lobbying disclosures, foreign-agent registrations,
   campaign-finance records, legal-entity registries, published enforcement outcomes, and statutes.
2. **Identity resolution.** Records are joined only through official identifiers or two agreeing facts, with
   explicit handling of nicknames, initials, maiden and married names, and reordered names. Uncertain matches are
   held for a human, with the evidence shown.
3. **The map.** A property graph of organizations, people and the documented relationships between them, each
   relationship carrying its citation, dates, status (current or historical) and fingerprint.
4. **Balance measures,** each tied to a published public standard, never to an undisclosed formula: board
   independence (stock-exchange listing rules), concentration of ownership (federal merger guidelines'
   concentration index), director tenure and commitments (published governance codes and proxy-voting
   guidelines), and structural single points of failure in the network.
5. **Pattern signals.** Connections that no single document states, found by combining official records: for
   example, a lobbyist who disclosed working for a legislator now lobbying in that legislator's committee's
   jurisdiction. Every signal lists the records it rests on and is worded as what the records show, never as motive.
6. **Rulebook.** For each sector, the laws, regulations and standards that apply, what each requires, and **what
   meeting it well looks like**, with links to the official text, checked automatically.

## 4. Reporting without harm

- **Two passes, every time.** A report is drafted, then independently re-checked: every source is re-opened and
  must name both parties; every record must be unchanged; the wording is screened for accusations, legal
  conclusions, motive, overstated certainty and private details; protective sections must be present.
- **Attributed wording.** Findings read "filings report X as director of Y", not "X is…".
- **Right of reply.** Every organization a finding concerns is invited to respond before publication, and the
  response is published.
- **Public-record enforcement only.** Enforcement history appears only as already published by the agency,
  quoted, with the agency's own qualifications (for example, that a settlement involves no determination of
  liability).
- **Whistleblower safeguard.** If an analysis suggests a possible new fraud against public funds, the report is
  held: such matters belong in a sealed legal process, not a publication.
- **Corrections in the open,** dated, with prior versions preserved.

## 5. From flags to help

Every measure maps to a rule in the rulebook, and every rule records practical steps an organization can take:
publishing related-party policies, refreshing long-tenured boards, screening board appointments for competitor
interlocks, disclosing lobbying positions, adopting conflict-of-interest policies. The long-term aim is a tool that
organizations can run on themselves: an assurance check that shows where they stand against public standards and
how to improve, before anyone else has to point it out.

## 6. Limits

Filings show roles and holdings, not intent or influence actually exercised. Coverage depends on what each
jurisdiction discloses; some registries are closed. Classifications such as sector labels are investigative
judgments and are labelled as such. Signals are for review, not findings.

## 7. Roadmap

Further official sources (federal awards, environmental and workplace-safety records, health-industry payment
disclosures, additional national registries); an organization-facing self-assessment built on the rulebook;
independent methodological review.

---

© 2026 Priority Nexus LLC. Priority Nexus™ is a trademark of Priority Nexus LLC.
Licensed under the **Apache License 2.0** (use freely, with credit to Priority Nexus LLC). Priority Nexus's own
threshold configurations, data, reports and keys are not part of any public release.

Part of the Priority Nexus Deterministic Governance & Circuit-Breaker Architecture:
https://github.com/PriorityNexusLLC/Deterministic-Governance-Circuit-Breaker-Architecture · Working implementation:
https://github.com/PriorityNexusLLC/PNMaster-Graph
