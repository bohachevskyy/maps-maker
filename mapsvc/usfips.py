"""US state FIPS codes and postal abbreviations. A static table, no I/O.

Census geography is keyed by two-digit state FIPS; everything else here keys
states by ISO 3166-2 ("US-CA"), whose second part is the USPS code.
"""

STATES = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY",
    # Territories: in Census files, but ISO gives them country codes too.
    "60": "AS", "66": "GU", "69": "MP", "72": "PR", "78": "VI",
}
POSTAL = {postal: fips for fips, postal in STATES.items()}
# For titles: "Median household income, California".
NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "AS": "American Samoa", "GU": "Guam",
    "MP": "Northern Mariana Islands", "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
}


def iso_3166_2(state_fips: str) -> str | None:
    postal = STATES.get(state_fips)
    return f"US-{postal}" if postal else None


def fips_of(code: str) -> str | None:
    """'US-CA' or 'CA' or '06' -> '06'."""
    code = code.strip().upper()
    if code.startswith("US-"):
        code = code[3:]
    if code in POSTAL:
        return POSTAL[code]
    return code if code in STATES else None
