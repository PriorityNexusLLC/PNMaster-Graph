"""Governance gate hooks.

These are deliberately simple, replaceable defaults. Priority Nexus's own threshold equations are
not part of this repository; to plug them in, replace these functions (same signatures) or point
`GATES` at another module. Everything else in the engine calls through here."""


def edge_cost(edge):
    """Traversal cost used by the 'strongest ties' path mode. Default: stronger documented
    conflict weight = cheaper hop. Must return a positive number."""
    w = float(((edge.get("integrity") or {}).get("conflict_weight")) or 0.0)
    return 1.0 - w + 0.01


def crossing_weight(edge, from_domains, to_domains):
    """Measured weight of a domain-boundary crossing. Default: the edge's recorded conflict_weight."""
    return float(((edge.get("integrity") or {}).get("conflict_weight")) or 0.0)


# Balance checks (engine/balance.py). Every default below is a published public standard, named with its
# source, or a plainly labelled display floor. They are not Priority Nexus thresholds; replace the values
# (keep the keys) to apply your own.
BALANCE_STANDARDS = {
    "independence_min_share": (0.5, "NYSE Listed Company Manual 303A.01 and Nasdaq Rule 5605(b)(1): "
                                     "a majority of the board must be independent directors"),
    "concentration_hhi_high": (1800, "U.S. DOJ/FTC Merger Guidelines (2023), Guideline 1: an HHI above 1,800 "
                                     "marks a highly concentrated market"),
    "tenure_years": (9, "UK Corporate Governance Code (2018), provision 10: more than nine years on a board "
                        "is a circumstance likely to impair a director's independence"),
    "overboard_seats": (5, "ISS U.S. Proxy Voting Guidelines (board composition): generally vote against directors who sit "
                           "on more than five public company boards"),
    "overboard_ceo_outside": (2, "ISS U.S. Proxy Voting Guidelines: a sitting CEO should serve on no more than two "
                                 "outside public company boards"),
    "min_board_size": (3, "display floor, not a standard: boards with fewer recorded directors are not scored"),
    "min_positions": (10, "display floor, not a standard: domains with fewer recorded ownership blocks are not scored"),
    "single_point_min_cut": (3, "display floor, not a standard: only list entities whose removal cuts off at least this many"),
}


def balance_standard(key):
    """(value, basis) for a balance check."""
    return BALANCE_STANDARDS[key]
