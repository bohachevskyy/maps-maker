"""Turning a column of values into k bins. Pure computation, no I/O."""

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

    def bin_of(self, value) -> int:
        if self.kind == "categorical":
            return self.categories.index(value)
        # Lower-closed bins; the top bin is closed at both ends so max lands inside.
        return min(bisect_right(self.breaks[1:-1], value), self.k - 1)

    def labels(self) -> list[str]:
        if self.kind == "categorical":
            return list(self.categories)
        out = []
        for i in range(self.k):
            lo, hi = self.breaks[i], self.breaks[i + 1]
            # A bin holding a single distinct value reads better as that value.
            out.append(format_value(lo) if lo == hi
                       else f"{format_value(lo)} – {format_value(hi)}")
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
