"""ColorBrewer palettes, tagged by kind.

The `kind` tag is what makes correctness rule #1 enforceable: a nominal
variable shaded with a sequential ramp implies an ordering that does not
exist, so the validator needs to know which schemes carry an ordering.
"""

# Sequential and diverging schemes are defined per class-count; ColorBrewer
# rebalances the whole ramp as k changes rather than just sampling more of it.
_GRADED = {
    "YlGnBu": ("sequential", {
        3: ["#edf8b1", "#7fcdbb", "#2c7fb8"],
        4: ["#ffffcc", "#a1dab4", "#41b6c4", "#225ea8"],
        5: ["#ffffcc", "#a1dab4", "#41b6c4", "#2c7fb8", "#253494"],
        6: ["#ffffcc", "#c7e9b4", "#7fcdbb", "#41b6c4", "#2c7fb8", "#253494"],
        7: ["#ffffcc", "#c7e9b4", "#7fcdbb", "#41b6c4", "#1d91c0", "#225ea8", "#0c2c84"],
        8: ["#ffffd9", "#edf8b1", "#c7e9b4", "#7fcdbb", "#41b6c4", "#1d91c0", "#225ea8", "#0c2c84"],
        9: ["#ffffd9", "#edf8b1", "#c7e9b4", "#7fcdbb", "#41b6c4", "#1d91c0", "#225ea8", "#253494", "#081d58"],
    }),
    "YlOrRd": ("sequential", {
        3: ["#ffeda0", "#feb24c", "#f03b20"],
        4: ["#ffffb2", "#fecc5c", "#fd8d3c", "#e31a1c"],
        5: ["#ffffb2", "#fecc5c", "#fd8d3c", "#f03b20", "#bd0026"],
        6: ["#ffffb2", "#fed976", "#feb24c", "#fd8d3c", "#f03b20", "#bd0026"],
        7: ["#ffffb2", "#fed976", "#feb24c", "#fd8d3c", "#fc4e2a", "#e31a1c", "#b10026"],
        8: ["#ffffcc", "#ffeda0", "#fed976", "#feb24c", "#fd8d3c", "#fc4e2a", "#e31a1c", "#b10026"],
        9: ["#ffffcc", "#ffeda0", "#fed976", "#feb24c", "#fd8d3c", "#fc4e2a", "#e31a1c", "#bd0026", "#800026"],
    }),
    "Blues": ("sequential", {
        3: ["#deebf7", "#9ecae1", "#3182bd"],
        4: ["#eff3ff", "#bdd7e7", "#6baed6", "#2171b5"],
        5: ["#eff3ff", "#bdd7e7", "#6baed6", "#3182bd", "#08519c"],
        6: ["#eff3ff", "#c6dbef", "#9ecae1", "#6baed6", "#3182bd", "#08519c"],
        7: ["#eff3ff", "#c6dbef", "#9ecae1", "#6baed6", "#4292c6", "#2171b5", "#084594"],
        8: ["#f7fbff", "#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#4292c6", "#2171b5", "#084594"],
        9: ["#f7fbff", "#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#4292c6", "#2171b5", "#08519c", "#08306b"],
    }),
    "Greens": ("sequential", {
        3: ["#e5f5e0", "#a1d99b", "#31a354"],
        4: ["#edf8e9", "#bae4b3", "#74c476", "#238b45"],
        5: ["#edf8e9", "#bae4b3", "#74c476", "#31a354", "#006d2c"],
        6: ["#edf8e9", "#c7e9c0", "#a1d99b", "#74c476", "#31a354", "#006d2c"],
        7: ["#edf8e9", "#c7e9c0", "#a1d99b", "#74c476", "#41ab5d", "#238b45", "#005a32"],
        8: ["#f7fcf5", "#e5f5e0", "#c7e9c0", "#a1d99b", "#74c476", "#41ab5d", "#238b45", "#005a32"],
        9: ["#f7fcf5", "#e5f5e0", "#c7e9c0", "#a1d99b", "#74c476", "#41ab5d", "#238b45", "#006d2c", "#00441b"],
    }),
    "PuBuGn": ("sequential", {
        3: ["#ece2f0", "#a6bddb", "#1c9099"],
        4: ["#f6eff7", "#bdc9e1", "#67a9cf", "#02818a"],
        5: ["#f6eff7", "#bdc9e1", "#67a9cf", "#1c9099", "#016c59"],
        6: ["#f6eff7", "#d0d1e6", "#a6bddb", "#67a9cf", "#1c9099", "#016c59"],
        7: ["#f6eff7", "#d0d1e6", "#a6bddb", "#67a9cf", "#3690c0", "#02818a", "#016450"],
        8: ["#fff7fb", "#ece2f0", "#d0d1e6", "#a6bddb", "#67a9cf", "#3690c0", "#02818a", "#016450"],
        9: ["#fff7fb", "#ece2f0", "#d0d1e6", "#a6bddb", "#67a9cf", "#3690c0", "#02818a", "#016c59", "#014636"],
    }),
    "RdBu": ("diverging", {
        3: ["#ef8a62", "#f7f7f7", "#67a9cf"],
        4: ["#ca0020", "#f4a582", "#92c5de", "#0571b0"],
        5: ["#ca0020", "#f4a582", "#f7f7f7", "#92c5de", "#0571b0"],
        6: ["#b2182b", "#ef8a62", "#fddbc7", "#d1e5f0", "#67a9cf", "#2166ac"],
        7: ["#b2182b", "#ef8a62", "#fddbc7", "#f7f7f7", "#d1e5f0", "#67a9cf", "#2166ac"],
        8: ["#b2182b", "#d6604d", "#f4a582", "#fddbc7", "#d1e5f0", "#92c5de", "#4393c3", "#2166ac"],
        9: ["#b2182b", "#d6604d", "#f4a582", "#fddbc7", "#f7f7f7", "#d1e5f0", "#92c5de", "#4393c3", "#2166ac"],
    }),
    "BrBG": ("diverging", {
        3: ["#d8b365", "#f5f5f5", "#5ab4ac"],
        4: ["#a6611a", "#dfc27d", "#80cdc1", "#018571"],
        5: ["#a6611a", "#dfc27d", "#f5f5f5", "#80cdc1", "#018571"],
        6: ["#8c510a", "#d8b365", "#f6e8c3", "#c7eae5", "#5ab4ac", "#01665e"],
        7: ["#8c510a", "#d8b365", "#f6e8c3", "#f5f5f5", "#c7eae5", "#5ab4ac", "#01665e"],
        8: ["#8c510a", "#bf812d", "#dfc27d", "#f6e8c3", "#c7eae5", "#80cdc1", "#35978f", "#01665e"],
        9: ["#8c510a", "#bf812d", "#dfc27d", "#f6e8c3", "#f5f5f5", "#c7eae5", "#80cdc1", "#35978f", "#01665e"],
    }),
}

# Qualitative schemes are a fixed cycle of distinguishable hues; take the first n.
_QUALITATIVE = {
    "Set2":   ["#66c2a5", "#fc8d62", "#8da0cb", "#e78ac3", "#a6d854", "#ffd92f", "#e5c494", "#b3b3b3"],
    "Dark2":  ["#1b9e77", "#d95f02", "#7570b3", "#e7298a", "#66a61e", "#e6ab02", "#a6761d", "#666666"],
    "Set3":   ["#8dd3c7", "#ffffb3", "#bebada", "#fb8072", "#80b1d3", "#fdb462",
               "#b3de69", "#fccde5", "#d9d9d9", "#bc80bd", "#ccebc5", "#ffed6f"],
    "Paired": ["#a6cee3", "#1f78b4", "#b2df8a", "#33a02c", "#fb9a99", "#e31a1c",
               "#fdbf6f", "#ff7f00", "#cab2d6", "#6a3d9a", "#ffff99", "#b15928"],
}

RAMPS = {name: kind for name, (kind, _) in _GRADED.items()}
RAMPS.update({name: "qualitative" for name in _QUALITATIVE})

# Missing values get their own visual channel -- never a ramp colour.
MISSING_GREY = "#d9d9d9"
HATCH_ID = "nodata-hatch"
HATCH_BG = "#f2f2f2"
HATCH_STROKE = "#8c8c8c"
BORDER = "#4d4d4d"
# A base map has no variable to encode, so it gets one neutral fill rather than
# a colour that might be read as meaning something.
BASEMAP_FILL = "#e8eef2"
OCEAN = "#ffffff"


class RampError(ValueError):
    """Raised when a ramp cannot supply the requested number of classes."""


def kind(name: str) -> str:
    """'sequential' | 'diverging' | 'qualitative'."""
    return RAMPS[name]


def max_classes(name: str) -> int:
    if name in _QUALITATIVE:
        return len(_QUALITATIVE[name])
    return max(_GRADED[name][1])


def colors(name: str, k: int) -> list[str]:
    """The k colours of `name`, darkest/last at the high end."""
    if name in _QUALITATIVE:
        palette = _QUALITATIVE[name]
        if k > len(palette):
            raise RampError(
                f"ramp '{name}' has {len(palette)} colours but {k} classes were requested; "
                "two categories would share a colour"
            )
        return list(palette[:k])
    table = _GRADED[name][1]
    smallest = min(table)
    if k < smallest:
        # ColorBrewer starts at three classes, but a region can legitimately
        # hold fewer distinct values -- a single-country map has exactly one.
        # Sample the ends of the smallest ramp so the order still reads.
        base = table[smallest]
        if k <= 1:
            return [base[-1]]
        return [base[round(i * (len(base) - 1) / (k - 1))] for i in range(k)]
    if k not in table:
        raise RampError(f"ramp '{name}' supports up to {max(table)} classes, got {k}")
    return list(table[k])
