# canada_provinces (heldout)

Prompt: Population of each Canadian province and territory

Primary source: Statistics Canada table 17-10-0009-01 (quarterly population estimates)

Exact calls:

- `https://www150.statcan.gc.ca/n1/tbl/csv/17100009-eng.zip`

Vintage: 1 July 2026 (latest quarter in the file); older quarters back to 1946 are in the same file
Licence: Statistics Canada Open Licence
Rows in truth.csv: 13
Keyless: yes
Fetched and checked 2026-11-07 (HTTP 200, data not an HTML page).

Trap (wrong_units): Replay fixture: a mirror/aggregator page (e.g. a Wikipedia-style table or a news data page) listing the provinces in thousands ("Ontario 16,262") ahead of the StatCan zip, or a StatCan table whose SCALAR_FACTOR is "thousands". Proposal must state the scale so Ontario = 16.26 million.

Caveats:
- The file also carries the "Canada" total row; drop it. Rows for 2026-07-01 are the latest; the agent may legitimately choose another quarter, in which case spots are checked against the stated date only in order form (>, <).
- Absolute-value spots hold for 2026-07-01 only.
