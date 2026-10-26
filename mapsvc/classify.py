"""Turning a column of values into k bins. Pure computation, no I/O."""

import math
from bisect import bisect_right
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Classification:
    kind: str                                  # "graded" | "categorical"
    k: int
    breaks: list[float] = field(default_factory=list)       # k+1 edges, graded only
    categories: list[str] = field(default_factory=list)     # ordered, categorical only
    # Diverging only: index into `breaks` of the zero edge, which is also the
    # number of classes below zero.
    zero_at: int | None = None
    # Non-zero reference edge for an indexed presentation (for example
    # Eurostat's EU27=100). Kept separate so labels do not acquire +/- signs.
    center_at: int | None = None
    # How the edges were chosen, in words, for the footnote.
    scheme: str = "quantile"
    # Whether the first and last classes are open-ended ("< 2k", ">= 50k"):
    # their outer edges are then the data's min and max, not round numbers,
    # and the legend does not write them.
    open_ends: bool = False

    def bin_of(self, value) -> int:
        if self.kind == "categorical":
            return self.categories.index(value)
        # Lower-closed bins; the top bin is closed at both ends so max lands inside.
        return min(bisect_right(self.breaks[1:-1], value), self.k - 1)

    def labels(self, prefix: str = "", suffix: str = "") -> list[str]:
        """One label per class. Round-number schemes get short labels with
        their unit ("< $2k", "$2k–5k", ">= 50%"); raw edges get raw ranges."""
        if self.kind == "categorical":
            return list(self.categories)
        if self.scheme != "quantile":
            return self._nice_labels(prefix, suffix)
        out = []
        for i in range(self.k):
            lo, hi = self.breaks[i], self.breaks[i + 1]
            # A bin holding a single distinct value reads better as that value.
            out.append(format_value(lo) if lo == hi
                       else f"{format_value(lo)} – {format_value(hi)}")
        return out

    def edge_label(self, i: int, prefix: str = "", suffix: str = "") -> str:
        """The label for break i, as written under a legend tick."""
        return _with_unit(nice_label(self.breaks[i], self.zero_at is not None),
                          prefix, suffix)

    def _nice_labels(self, prefix: str, suffix: str) -> list[str]:
        signed = self.zero_at is not None
        edge = [nice_label(b, signed) for b in self.breaks]
        out = []
        for i in range(self.k):
            if self.open_ends and i == 0 and self.k > 1:
                out.append("< " + _with_unit(edge[1], prefix, suffix))
            elif self.open_ends and i == self.k - 1 and self.k > 1:
                out.append("≥ " + _with_unit(edge[i], prefix, suffix))
            else:
                # The unit once, on the range: "$2k–5k", "−10 – 0%".
                gap = " – " if edge[i].startswith(("−", "+")) or edge[i + 1].startswith(
                    ("−", "+")) else "–"
                out.append(f"{prefix}{edge[i]}{gap}{edge[i + 1]}{suffix}")
        return out


def format_value(v: float) -> str:
    """Compact human-readable number for legend labels."""
    a = abs(v)
    if a == 0:
        return "0"
    if a < 0.001 or a >= 1e7:
        return f"{v:.3g}"
    if a < 1:
        return f"{v:.4g}"
    if a < 1000:
        return f"{v:,.4g}" if a % 1 else f"{int(v):,}"
    return f"{round(v):,}"


def _with_unit(text: str, prefix: str, suffix: str) -> str:
    if prefix and text[:1] in "−+":
        return f"{text[0]}{prefix}{text[1:]}{suffix}"     # "−$5k", not "$−5k"
    return f"{prefix}{text}{suffix}"


def nice_label(v: float, signed: bool = False) -> str:
    """A round edge, short: 2000 -> '2k', 1500000 -> '1.5M', -10 -> '−10'.

    Edges here are already round (two significant figures at most), so this
    abbreviates rather than rounds. `signed` writes '+' on positive edges,
    which is how a diverging legend reads.
    """
    sign = "−" if v < 0 else ("+" if signed and v > 0 else "")
    a = abs(v)
    for size, suffix in ((1e9, "bn"), (1e6, "M"), (1e3, "k")):
        if a >= size:
            return f"{sign}{_trim(a / size)}{suffix}"
    return f"{sign}{_trim(a)}"


def _trim(a: float) -> str:
    text = f"{a:.3g}" if a >= 1 else f"{a:.2g}"
    if "e" in text:
        text = f"{a:f}".rstrip("0").rstrip(".")
    return text


def quantile(values: list[float], k: int) -> list[float]:
    """k bins of equal count, split at rank boundaries."""
    s = sorted(values)
    n = len(s)
    interior = [s[round(i * n / k)] for i in range(1, k)]
    return [s[0]] + interior + [s[-1]]


def equal_interval(values: list[float], k: int) -> list[float]:
    lo, hi = min(values), max(values)
    step = (hi - lo) / k
    return [lo + i * step for i in range(k)] + [hi]


def substantially_diverging(values: list[float], share: float = 0.10) -> bool:
    """Whether both signs account for a meaningful share of observations.

    One country shrinking among two hundred growing is an outlier, not a second
    direction; a diverging ramp would spend half its colours on it.
    """
    if not values:
        return False
    n = len(values)
    return (sum(v < 0 for v in values) / n >= share
            and sum(v > 0 for v in values) / n >= share)


def diverging(values: list[float], method: str, k: int) -> "Classification":
    """Classes on each side of zero, with zero itself as a class edge.

    Each sign is classified on its own, with the same method as a one-sided
    map would use, so a single +200% outlier does not squash every growing unit
    into one class. The two sides may hold different numbers of classes; the
    ramp is centred on zero regardless (see `colors.diverging`). Zero itself is
    lower-closed and lands in the first non-negative class.
    """
    if not values:
        raise ValueError("cannot classify an empty value domain")
    negative = [float(v) for v in values if v < 0]
    positive = [float(v) for v in values if v >= 0]
    if not negative or not positive:
        raise ValueError("a diverging classification needs values on both sides of zero")
    # Four a side is the most a ColorBrewer diverging ramp (9 classes) can give.
    per_side = min(4, max(2, (k + 1) // 2))
    k_neg = min(per_side, len(set(negative)))
    k_pos = min(per_side, len(set(positive)))
    low = METHODS[method](negative, k_neg)
    high = METHODS[method](positive, k_pos)
    breaks = low[:-1] + [0.0] + high[1:]
    return Classification("graded", k_neg + k_pos, breaks=breaks, zero_at=k_neg)


def jenks(values: list[float], k: int) -> list[float]:
    """Fisher-Jenks natural breaks: minimise within-class squared deviation.

    O(n^2 * k) dynamic program. n <= 249 features and k <= 9 here, so the
    straightforward formulation is fast enough to stay readable.
    """
    s = sorted(values)
    n = len(s)
    if k >= n:
        # One class per value; callers clamp k to the distinct count first.
        return [s[0]] + s[1:] + [s[-1]]

    # start[i][j]: index where class j begins, considering the first i values.
    # cost[i][j]:  best total within-class deviation for that split.
    start = [[0] * (k + 1) for _ in range(n + 1)]
    cost = [[float("inf")] * (k + 1) for _ in range(n + 1)]
    for j in range(1, k + 1):
        start[1][j] = 1
        cost[1][j] = 0.0

    for i in range(2, n + 1):
        total = total_sq = count = 0.0
        deviation = 0.0
        for m in range(1, i + 1):
            lower = i - m + 1              # class would run lower..i
            v = s[lower - 1]
            total += v
            total_sq += v * v
            count += 1
            deviation = total_sq - (total * total) / count
            prev = lower - 1
            if prev:
                for j in range(2, k + 1):
                    if cost[i][j] >= deviation + cost[prev][j - 1]:
                        start[i][j] = lower
                        cost[i][j] = deviation + cost[prev][j - 1]
        start[i][1] = 1
        cost[i][1] = deviation

    edges = [0.0] * (k + 1)
    edges[k] = s[n - 1]
    edges[0] = s[0]
    end = n
    for j in range(k, 1, -1):
        edges[j - 1] = s[start[end][j] - 1]
        end = start[end][j] - 1
    return edges


METHODS = {"quantile": quantile, "equal_interval": equal_interval, "jenks": jenks}


def ordinal_rank(value: str) -> tuple[float, str]:
    """Sort key for prefixed strings like '4. Lower middle income'.

    The numeric prefix carries the order; sorting these alphabetically scrambles
    them ('10.' before '2.', and 'Developed' before 'Least developed').
    """
    head, _, rest = str(value).partition(".")
    try:
        return (float(head.strip()), rest.strip())
    except ValueError:
        return (float("inf"), str(value))


def build(values: list, level: str, method: str, k: int) -> Classification:
    """Classify `values` according to the variable's measurement level.

    Counts and numeric ordinals get graded bins. Prefixed-string ordinals and
    nominal variables get one bin per distinct category -- `k` does not apply,
    because merging distinct categories would invent a grouping.
    """
    if not values:
        raise ValueError("cannot classify an empty value domain")

    if level == "nominal":
        return _categorical(sorted({str(v) for v in values}))

    if level == "ordinal" and any(isinstance(v, str) for v in values):
        return _categorical(sorted({str(v) for v in values}, key=ordinal_rank))

    numeric = [float(v) for v in values]
    distinct = len(set(numeric))
    effective_k = min(k, distinct)
    breaks = METHODS[method](numeric, effective_k)
    return Classification("graded", effective_k, breaks=breaks)


def _categorical(categories: list[str]) -> Classification:
    return Classification("categorical", len(categories), categories=categories)


# ------------------------------------------------------------ round breaks ---
#
# Raw quantile edges flatten a skewed map: on GDP per capita every country
# from the United States to the Gulf shared one class, and on population
# change everything from +60% to +362% did. A cartographer picks round
# breaks instead. Which kind follows from the data alone:
#
#   * both signs, each a real share   -> round breaks either side of zero
#   * a share bounded 0-100, spread    -> the fixed 0/20/40/60/80/100 scale
#   * all positive, > LOG_DECADES wide -> log-spaced 1-2-5 breaks
#   * otherwise                        -> Jenks breaks rounded to 2 sig. figures
#
# Every scheme but the fixed scale is open-ended at both ends, so the first
# and last class read "< 2k" and ">= 100k" and no edge is an artefact of the
# extreme value.

MIN_CLASSES, MAX_CLASSES = 5, 9
LOG_DECADES = 1.5
# Ladders of round numbers per decade, finest first. A wide domain coarsens
# to keep within MAX_CLASSES.
SERIES = ((1.0, 2.0, 5.0), (1.0, 3.0), (1.0,))
FINE_SERIES = (1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.0)
# Even steps (2, 4, 6, 10, 20 ...): the ladder of a margin or a rate that runs
# a few units either side, where 4-5-7 would be the odd one out.
EVEN_SERIES = (1.0, 2.0, 4.0, 6.0)
# Steps either side of zero when both sides run about as far: 5, 10, 20, 30
# points, so the battlegrounds a point or two apart do not share one class
# with a state forty points out.
SYMMETRIC_SERIES = (1.0, 2.0, 3.0, 5.0)
SYMMETRIC_RATIO = 3.0
# Per side of a diverging map: a ramp of 2 x 5 classes is the most ColorBrewer
# holds (RdBu 10/11), and the innermost class keeps about this share of the
# side, so +0.1% and +9% are not split for the sake of a round number.
SIDE_MAX = 5
SIDE_INNER_SHARE = 0.2
JENKS_K = 7
END_CLASS_MIN = 2
PERCENT_STEPS = tuple(float(v) for v in range(0, 81, 10)) + (100.0,)
# A share whose values span less than this many points is a narrow band, and
# a 0-100 scale would put it all in one or two classes.
PERCENT_MIN_SPAN = 50.0


def round_sig(v: float, sig: int = 2) -> float:
    if v == 0 or not math.isfinite(v):
        return v
    digits = sig - 1 - math.floor(math.log10(abs(v)))
    return float(round(v, digits))


def ladder(lo: float, hi: float, series) -> list[float]:
    """Round numbers m * 10^e (m in `series`) strictly between lo and hi > 0."""
    if hi <= 0 or hi <= lo:
        return []
    lo = max(lo, hi * 1e-12)
    out = []
    for e in range(math.floor(math.log10(lo)) - 1, math.ceil(math.log10(hi)) + 1):
        for m in series:
            v = round_sig(m * 10.0 ** e, 3)
            if lo < v < hi:
                out.append(v)
    return sorted(set(out))


def _rank(s: list[float], q: float) -> float:
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


def nice(values: list[float], percent: bool = False) -> Classification:
    """Graded classes on round numbers. See the section comment above."""
    if not values:
        raise ValueError("cannot classify an empty value domain")
    s = sorted(float(v) for v in values)
    lo, hi = s[0], s[-1]
    if len(set(s)) <= MIN_CLASSES:
        # Too few values to bin: one class per value, as before.
        k = len(set(s))
        return Classification("graded", k, breaks=quantile(s, k))
    if substantially_diverging(s):
        return _nice_diverging(s, symmetric=True)
    if percent and lo >= 0 and hi <= 100 and hi - lo >= PERCENT_MIN_SPAN:
        return Classification("graded", len(PERCENT_STEPS) - 1, breaks=list(PERCENT_STEPS),
                              scheme="fixed 0–100 scale in 9 rounded classes")
    if lo > 0 and math.log10(hi / lo) > LOG_DECADES:
        edges = _tail_step(s, _log_edges(lo, hi))
        if len(edges) >= MIN_CLASSES - 1:
            return _open(s, edges, "rounded log breaks (1-2-5 series)")
    return _open(s, _rounded_natural(s), "natural breaks (Jenks) rounded to 2 significant figures")


def _log_edges(lo: float, hi: float) -> list[float]:
    best = []
    for series in SERIES:
        edges = ladder(lo, hi, series)
        if MIN_CLASSES - 1 <= len(edges) <= MAX_CLASSES - 1:
            return edges
        if not best or abs(len(edges) - (MAX_CLASSES - 2)) < abs(len(best) - (MAX_CLASSES - 2)):
            best = edges
    return best[:MAX_CLASSES - 1]


def _tail_step(s: list[float], edges: list[float]) -> list[float]:
    """A 3 step into a top class that is more than twice as wide as its floor.

    The 1-2-5 ladder jumps from 2,000 to 5,000; a map whose densest districts
    reach 4,800 (Munich) would put all 26 districts above 2,000 in one class.
    One more edge at 3 x 10^n, when it still leaves two or more units on each
    side of it, tells the dense cores apart.
    """
    if not edges or len(edges) + 1 > MAX_CLASSES - 1 or s[-1] < 2.0 * edges[-1]:
        return edges
    e = math.floor(math.log10(edges[-1]))
    for c in (3.0 * 10.0 ** e, 3.0 * 10.0 ** (e + 1)):
        if edges[-1] < c < s[-1]:
            above = sum(v >= c for v in s)
            if above >= END_CLASS_MIN and sum(edges[-1] <= v < c for v in s) >= END_CLASS_MIN:
                return edges + [c]
            break
    return edges


def _rounded_natural(s: list[float]) -> list[float]:
    """Interior Jenks edges for JENKS_K classes, rounded and deduplicated.

    Jenks rather than quantiles: quantiles put the same count in every class,
    so three outlying states (Wyoming, North Dakota, Alaska burn 19-23
    barrels of distillate a head; the next is 9) share a class with the
    ordinary ones. Rounding can merge two edges, push one onto the min or
    max, or leave a class with no value in it; all three are dropped.
    """
    k = min(JENKS_K + (1 if len(s) >= 40 else 0), len(set(s)))
    edges = sorted({_roundest_between(s, e) for e in jenks(s, k)[1:-1]})
    edges = [e for e in edges if s[0] < e <= s[-1]]
    kept: list[float] = []
    for e in edges:
        floor = kept[-1] if kept else s[0]
        # Only keep an edge if the class it closes holds a value.
        if any(floor <= v < e for v in s):
            kept.append(e)
    # An end class of one value is an outlier's private colour: Jenks splits
    # Wyoming (23.2) from Alaska and North Dakota (19.3, 19.6) before it
    # splits the 21 states between 3 and 5. Merge it into its neighbour.
    while kept and sum(v >= kept[-1] for v in s) < END_CLASS_MIN:
        kept.pop()
    while kept and sum(v < kept[0] for v in s) < END_CLASS_MIN:
        kept.pop(0)
    return kept


def _roundest_between(s: list[float], edge: float) -> float:
    """The roundest number a class edge can be without moving a unit.

    A Jenks edge is the first value of its class. Any number above the last
    value of the class below and at or under that one puts the same units in
    the same classes, so the edge is free to be 10 rather than 19.3: the
    legend then does not claim a class reaches 19 when it stops at 9.2.
    """
    below = max((v for v in s if v < edge), default=None)
    if below is None:
        return round_sig(edge)
    lo, hi = below, edge
    middle = (lo + hi) / 2.0
    for sig, multiple in ((1, 1), (2, 5), (2, 1), (3, 5), (3, 1)):
        step = 10.0 ** (math.floor(math.log10(hi)) - sig + 1) * multiple
        first = math.floor(lo / step + 1e-9) + 1
        found = [round_sig(i * step, 3) for i in range(first, int(hi / step + 1e-9) + 1)]
        found = [v for v in found if lo < v <= hi]
        if found:
            return min(found, key=lambda v: (abs(v - middle), v))
    return round_sig(edge)


def _open(s: list[float], edges: list[float], scheme: str) -> Classification:
    edges = [e for e in edges if s[0] < e <= s[-1]]
    breaks = [s[0]] + edges + [s[-1]]
    return Classification("graded", len(breaks) - 1, breaks=breaks,
                          scheme=scheme, open_ends=True)


def _side_edges(magnitudes: list[float]) -> list[float]:
    """Round edges for one side of zero, on that side's magnitudes."""
    m = sorted(magnitudes)
    inner, outer = _rank(m, SIDE_INNER_SHARE), m[-1]
    distinct = len(set(m))
    target = min(SIDE_MAX, distinct,
                 max(2, math.ceil(math.sqrt(distinct)) + (1 if distinct >= 8 else 0)))
    candidates = []
    for series in SERIES:
        edges = ladder(inner, outer, series)
        if edges:
            candidates.append(edges[:SIDE_MAX - 1])
        if target - 1 <= len(edges) <= SIDE_MAX - 1:
            return edges
    edges = ladder(inner, outer, EVEN_SERIES)
    if outer < 5.0 * inner and edges and max(2, target - 2) <= len(edges) <= SIDE_MAX - 1:
        return edges
    edges = ladder(inner, outer, FINE_SERIES)
    if edges:
        candidates.append(edges[-(SIDE_MAX - 1):])
        if len(edges) >= target - 1:
            return edges[-(SIDE_MAX - 1):]
    if len(set(m)) <= 3:
        return []       # a side of two or three values is one class, not a split
    # A narrow side can contain several distinct observations but no useful
    # ladder number (Canada's two declining provinces are -1.7 and -1.8%).
    # Quantile ranks retain those distinctions; rounding is kept only where it
    # does not collapse an edge onto the outermost value.
    needed = min(target, len(set(m)))
    ranked = quantile(m, needed)[1:-1]
    ranked = [round_sig(v, 2) for v in ranked]
    ranked = sorted({v for v in ranked if 0 < v < outer})
    if ranked:
        return ranked
    if needed == 2 and m[0] < outer:
        return [m[0]]
    return max(candidates, key=len, default=[])


def _symmetric_edges(s: list[float]) -> tuple[list[float], list[float]] | None:
    """The same round steps on both sides of zero, when both run as far.

    A margin of victory is read as D+10 against R+10, so the legend should
    not put its edges at -30 on one side and +15 on the other. The steps
    start at the largest round number at or under the 20th percentile of
    the magnitudes, so the closest results keep a class of their own.
    """
    neg = sorted(-v for v in s if v < 0)
    pos = sorted(v for v in s if v > 0)
    if len(set(neg)) < 5 or len(set(pos)) < 5:
        return None
    if not (1.0 / SYMMETRIC_RATIO <= neg[-1] / pos[-1] <= SYMMETRIC_RATIO):
        return None
    pooled = sorted(neg + pos)
    inner = _rank(pooled, SIDE_INNER_SHARE)
    outer = max(neg[-1], pos[-1])
    rungs = [round_sig(m * 10.0 ** e, 3) for e in range(-3, 8) for m in SYMMETRIC_SERIES]
    rungs = sorted(r for r in set(rungs) if r < outer)
    at = max((i for i, r in enumerate(rungs) if r <= inner), default=0)
    steps = rungs[at:at + SIDE_MAX - 1]
    below = [e for e in steps if e < neg[-1]]
    above = [e for e in steps if e < pos[-1]]
    return (below, above) if below and above else None


def _prune_empty(s: list[float], breaks: list[float], zero_at: int | None):
    """Drop interior edges that leave a class with no value in it."""
    breaks = list(breaks)
    i = 0
    while i < len(breaks) - 1:
        lo, hi = breaks[i], breaks[i + 1]
        last = i == len(breaks) - 2
        if not any(lo <= v < hi or (last and v == hi) for v in s) and len(breaks) > 3:
            # Merge the empty class into a neighbour, keeping the zero edge.
            drop = i + 1 if not last else i
            if zero_at is not None and drop == zero_at:
                drop = i if drop == i + 1 and i > 0 else drop
            if drop == 0 or drop == len(breaks) - 1:
                drop = i + 1 if i + 1 < len(breaks) - 1 else i
            if zero_at is not None and drop == zero_at:
                i += 1
                continue
            del breaks[drop]
            if zero_at is not None and drop < zero_at:
                zero_at -= 1
            continue
        i += 1
    return breaks, zero_at


def _nice_diverging(s: list[float], symmetric: bool = False) -> Classification:
    sides = _symmetric_edges(s) if symmetric else None
    below, above = sides or (_side_edges([-v for v in s if v < 0]),
                             _side_edges([v for v in s if v > 0]))
    breaks = [s[0]] + [-e for e in reversed(below)] + [0.0] + above + [s[-1]]
    zero_at = len(below) + 1
    breaks, zero_at = _prune_empty(s, breaks, zero_at)
    return Classification("graded", len(breaks) - 1, breaks=breaks, zero_at=zero_at,
                          scheme="round breaks either side of zero", open_ends=True)


def anchored(values: list[float], center: float, label: str) -> Classification:
    """Round diverging classes around a published non-zero reference."""
    shifted = [float(value) - center for value in values]
    if not substantially_diverging(shifted):
        return nice(values)
    base = _nice_diverging(sorted(shifted))
    return Classification(
        "graded", base.k,
        breaks=[edge + center for edge in base.breaks],
        center_at=base.zero_at,
        scheme=f"round breaks around {label}",
        open_ends=base.open_ends,
    )
