"""Lexicographic ranks (LexoRank-style) for manually ordered lists such as backlogs.

A rank is a string of base-36 digits (0-9, a-z). Items sort by plain string comparison, so moving an
item only rewrites that item's rank: ``between(prev_rank, next_rank)`` gives a rank strictly between
its new neighbours. Ranks never end in "0" (the smallest digit), which keeps a gap below every rank,
so there is always room to insert. Repeated inserts at the same spot grow ranks by about one character
per five inserts; ``spread`` re-spaces a whole list evenly when that gets long.
"""
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
BASE = len(DIGITS)
_INDEX = {c: i for i, c in enumerate(DIGITS)}


class RankError(ValueError):
    pass


def validate(rank: str) -> str:
    if not rank or any(c not in _INDEX for c in rank) or rank.endswith("0"):
        raise RankError(f"invalid rank {rank!r}: use digits 0-9a-z, not ending in 0")
    return rank


def between(lo: str = None, hi: str = None) -> str:
    """A rank strictly between ``lo`` and ``hi``; ``None`` means an open end (first/last position)."""
    if lo is not None:
        validate(lo)
    if hi is not None:
        validate(hi)
        if lo is not None and lo >= hi:
            raise RankError(f"no rank between {lo!r} and {hi!r}: lower bound is not below upper bound")
    # Appending (open top) steps just past lo and prepending (open bottom) just below hi, so runs of
    # appends/prepends grow ranks slowly; a bounded insert takes the midpoint.
    bias = "low" if hi is None and lo is not None else "high" if lo is None and hi is not None else "mid"
    out, i = [], 0
    while True:
        a = _INDEX[lo[i]] if lo is not None and i < len(lo) else 0
        b = _INDEX[hi[i]] if hi is not None and i < len(hi) else BASE
        if a == b:                       # shared prefix: copy and keep going
            out.append(DIGITS[a])
        elif b - a > 1:                  # room at this digit: pick one and stop
            out.append(DIGITS[a + 1 if bias == "low" else b - 1 if bias == "high" else (a + b) // 2])
            return "".join(out)
        else:                            # adjacent digits: take lo's, then anything above lo's tail fits
            out.append(DIGITS[a])
            hi = None
        i += 1


def spread(n: int, width: int = None) -> list:
    """``n`` evenly spaced ranks in ascending order (for a new list, or to re-space an existing one)."""
    if n <= 0:
        return []
    width = width or max(2, len(_to_base(n + 1)) + 1)
    step = BASE ** width // (n + 1)
    ranks = []
    for k in range(1, n + 1):
        r = _to_base(step * k).rjust(width, "0").rstrip("0")
        ranks.append(r)
    return ranks


def _to_base(n: int) -> str:
    s = ""
    while n:
        n, d = divmod(n, BASE)
        s = DIGITS[d] + s
    return s or "0"
