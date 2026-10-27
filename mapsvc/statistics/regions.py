"""Fixed country membership for named world regions.

The geographic regions follow UN M49's assignment of countries and separately
coded territories.  That convention assigns each code once: notably Russia is
in Eastern Europe and Turkey is in Western Asia.  Taiwan is included in Eastern
Asia under its ISO code so an ISO-keyed map does not silently lose it.

The few non-M49 groups below state their definitions explicitly.  Keeping this
table in code makes an agent's region list data, not geography it can improvise.
"""

import dataclasses
from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    name: str
    members: tuple[str, ...]
    convention: str

    @property
    def footnote(self) -> str:
        """The membership rule in one clause, for the map's footnote."""
        if self.convention == M49:
            return ("members per UN M49 (Russia counted in Europe, Turkey in "
                    "Asia, Taiwan in Eastern Asia)")
        return "members: " + self.convention.rstrip(".")


M49 = ("UN M49 geographic regions and sub-regions; every country or separately "
       "coded territory belongs to one sub-region. Russia is Europe; Turkey is "
       "Asia. Taiwan is represented by ISO code TWN in Eastern Asia.")

# Sub-regions. Codes are ISO 3166-1 alpha-3 because that is the service's join
# key; the membership is the UN M49 geographic classification (with TWN made
# explicit as documented above).
_SUBREGIONS = {
    "northern africa": "DZA EGY LBY MAR SDN TUN ESH",
    "eastern africa": "BDI COM DJI ERI ETH ATF IOT KEN MDG MWI MUS MYT MOZ REU RWA SYC SOM SSD UGA TZA ZMB ZWE",
    "middle africa": "AGO CMR CAF TCD COG COD GNQ GAB STP",
    "southern africa": "BWA SWZ LSO NAM ZAF",
    "western africa": "BEN BFA CPV CIV GMB GHA GIN GNB LBR MLI MRT NER NGA SHN SEN SLE TGO",
    "caribbean": "AIA ATG ABW BHS BRB BES VGB CYM CUB CUW DMA DOM GRD GLP HTI JAM MTQ MSR PRI BLM KNA LCA MAF VCT SXM TTO TCA VIR",
    "central america": "BLZ CRI SLV GTM HND MEX NIC PAN",
    "northern america": "BMU CAN GRL SPM USA",
    "south america": "ARG BOL BVT BRA CHL COL ECU FLK GUF GUY PRY PER SGS SUR URY VEN",
    "central asia": "KAZ KGZ TJK TKM UZB",
    "eastern asia": "CHN HKG MAC PRK JPN MNG KOR TWN",
    "south-eastern asia": "BRN KHM IDN LAO MYS MMR PHL SGP THA TLS VNM",
    "southern asia": "AFG BGD BTN IND IRN MDV NPL PAK LKA",
    "western asia": "ARM AZE BHR CYP GEO IRQ ISR JOR KWT LBN OMN QAT SAU PSE SYR TUR ARE YEM",
    "eastern europe": "BLR BGR CZE HUN POL MDA ROU RUS SVK UKR",
    "northern europe": "ALA DNK EST FRO FIN GGY ISL IRL IMN JEY LVA LTU NOR SJM SWE GBR",
    "southern europe": "ALB AND BIH KOS HRV GIB GRC VAT ITA MLT MNE MKD PRT SMR SRB SVN ESP",
    "western europe": "AUT BEL FRA DEU LIE LUX MCO NLD CHE",
    "australia and new zealand": "AUS CXR CCK HMD NZL NFK",
    "melanesia": "FJI NCL PNG SLB VUT",
    "micronesia": "GUM KIR MHL FSM NRU MNP PLW UMI",
    "polynesia": "ASM COK PYF NIU PCN WSM TKL TON TUV WLF",
}


def _codes(text: str) -> tuple[str, ...]:
    return tuple(sorted(text.split()))


REGIONS: dict[str, Region] = {
    name: Region(name.title(), _codes(codes), M49)
    for name, codes in _SUBREGIONS.items()
}


def _union(name: str, parts: tuple[str, ...]) -> None:
    members = tuple(sorted({code for part in parts for code in REGIONS[part].members}))
    REGIONS[name] = Region(name.title(), members, M49)


_union("africa", ("northern africa", "eastern africa", "middle africa",
                   "southern africa", "western africa"))
_union("americas", ("caribbean", "central america", "northern america",
                     "south america"))
_union("asia", ("central asia", "eastern asia", "south-eastern asia",
                 "southern asia", "western asia"))
_union("europe", ("eastern europe", "northern europe", "southern europe",
                   "western europe"))
_union("oceania", ("australia and new zealand", "melanesia", "micronesia",
                    "polynesia"))
_union("sub-saharan africa", ("eastern africa", "middle africa",
                              "southern africa", "western africa"))


DISPLAY = {"eu27": "EU27", "latin america and the caribbean": "Latin America and the Caribbean"}


def _informal(name: str, codes: str, definition: str) -> None:
    REGIONS[name] = Region(DISPLAY.get(name, name.title()), _codes(codes), definition)


_informal("eu27", "AUT BEL BGR HRV CYP CZE DNK EST FIN FRA DEU GRC HUN IRL ITA LVA LTU LUX MLT NLD POL PRT ROU SVK SVN ESP SWE",
          "European Union membership after the United Kingdom's withdrawal (EU27, 2020-present).")
REGIONS["european union"] = dataclasses.replace(REGIONS["eu27"], name="European Union")
_informal("scandinavia", "DNK NOR SWE",
          "Scandinavia in the strict common definition: Denmark, Norway and Sweden.")
_informal("nordics", "DNK FIN ISL NOR SWE",
          "Nordic sovereign states; dependent territories are omitted unless requested.")
_informal("balkans", "ALB BIH BGR HRV GRC KOS MNE MKD ROU SRB SVN",
          "Common inclusive political definition: Albania, Bosnia and Herzegovina, Bulgaria, Croatia, Greece, Kosovo, Montenegro, North Macedonia, Romania, Serbia and Slovenia.")
_informal("middle east", "BHR CYP EGY IRN IRQ ISR JOR KWT LBN OMN PSE QAT SAU SYR TUR ARE YEM",
          "Common broad definition: Western Asia plus Egypt and Iran, excluding the South Caucasus.")

ALIASES = {
    "eu": "eu27", "e.u.": "eu27",
    "latin america": "latin america and the caribbean",
    "east asia": "eastern asia", "southeast asia": "south-eastern asia",
    "south east asia": "south-eastern asia", "south asia": "southern asia",
    "west asia": "western asia", "north africa": "northern africa",
    "east africa": "eastern africa", "west africa": "western africa",
    "southern african": "southern africa", "eastern european": "eastern europe",
}
def _informal_union(name: str, parts: tuple[str, ...], definition: str) -> None:
    members = " ".join(code for part in parts for code in REGIONS[part].members)
    _informal(name, members, definition)


_informal_union("latin america and the caribbean",
                ("caribbean", "central america", "south america"),
                "UN M49 Latin America and the Caribbean grouping (Caribbean, "
                "Central America and South America).")
_informal_union("north america",
                ("northern america", "central america", "caribbean"),
                "Colloquial North America: UN M49 Northern America plus Central "
                "America (including Mexico) and the Caribbean.")


def names() -> tuple[str, ...]:
    return tuple(sorted(REGIONS))


def get(name: str) -> Region | None:
    key = " ".join(name.strip().lower().replace("_", " ").split())
    key = ALIASES.get(key, key)
    return REGIONS.get(key)


def identify(members) -> Region | None:
    """Return a uniquely matching named region, preferring common top-level names."""
    wanted = tuple(sorted(str(code).upper() for code in members or ()))
    preferred = ("eu27", "africa", "asia", "south america", "europe", "oceania",
                 "scandinavia", "nordics", "balkans", "middle east")
    for name in (*preferred, *names()):
        region = REGIONS[name]
        if region.members == wanted:
            return region
    return None
