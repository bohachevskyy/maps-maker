"""The manifest: what to map, how to classify it, how to colour it.

Validation lives here and runs before any I/O, so a bad request never reaches
the network or the cache.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Manifest:
    region: str
    level: str
    variable_source: str
    variable_id: str
    normalize: str | None
    method: str
    k: int
    ramp: str
    projection: str
    missing: str

    def data_key(self) -> dict:
        """The data-relevant fields only.

        `ramp` and `classify` are render-time decisions and must not invalidate
        a fetch, so they are deliberately absent.
        """
        return {
            "region": self.region,
            "level": self.level,
            "variable": {"source": self.variable_source, "id": self.variable_id},
            "normalize": self.normalize,
        }
