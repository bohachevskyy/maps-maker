"""Check that every spot in specs.toml is consistent with its truth.csv.

    uv run python evals/research/check_truth.py

Offline. Key column = first column; value column = last numeric column named
below (or the 4th for countries). Points/lines: count and event_near only.
"""
import csv, math, re, tomllib
from pathlib import Path

HERE = Path(__file__).parent
VAL = {  # case -> (key column, value column)
    'precip_country': ('ISO3', 'value'), 'freshwater_country': ('ISO3', 'value'),
    'refugees_asylum': ('ISO3', 'refugees'), 'refugees_origin': ('ISO3', 'refugees'),
    'brazil_states': ('ISO3166_2', 'population'), 'japan_prefectures': ('ISO3166_2', 'population_persons'),
    'mexico_states': ('ISO3166_2', 'pct_no_health_coverage'), 'canada_provinces': ('ISO3166_2', 'population'),
    'australia_states': ('ISO3166_2', 'population'), 'control_owid_lifeexp': ('ISO3', 'life_expectancy'),
    'control_eurostat_unemp': ('NUTS', 'unemployment_rate_pct'),
}
COORD = {'whs_points': ('lon', 'lat'), 'meteorites_fell': ('lon', 'lat'), 'canada_large_fires': ('lon', 'lat'),
         'airports_africa': ('lon', 'lat'), 'tornado_tracks': ('slon', 'slat'), 'hurricane_tracks_2005': ('lon', 'lat')}
NEAR_KM = 5.0


def km(a, b):
    p = math.pi / 180
    h = math.sin((b[1]-a[1])*p/2)**2 + math.cos(a[1]*p)*math.cos(b[1]*p)*math.sin((b[0]-a[0])*p/2)**2
    return 12742 * math.asin(math.sqrt(h))


bad = 0
for c in tomllib.load(open(HERE/'specs.toml', 'rb'))['case']:
    if c['should_refuse']:
        continue
    rows = list(csv.DictReader(open(HERE/c['id']/'truth.csv')))
    for s in c['spots']:
        ok = None
        if s.startswith('count='):
            m = re.match(r'count=(\d+)±(\d+)%', s); n, t = int(m[1]), int(m[2])
            cnt = len({r['sid'] for r in rows}) if c['id'] == 'hurricane_tracks_2005' else len(rows)
            cnt -= c['id'] == 'australia_states'  # drop Other Territories
            ok = abs(cnt-n) <= n*t/100
        elif s.startswith('event_near='):
            lo, la = (float(x) for x in s[11:].split(','))
            kx, ky = COORD[c['id']]
            ok = any(km((lo, la), (float(r[kx]), float(r[ky]))) < NEAR_KM for r in rows)
        else:
            k, v = VAL[c['id']]; d = {r[k]: float(r[v]) for r in rows}
            if m := re.match(r'(\S+)=([\d.]+)±([\d.]+)%$', s):
                ok = abs(d[m[1]]-float(m[2])) <= float(m[2])*float(m[3])/100
            elif m := re.match(r'(\S+)([<>])(\S+)$', s):
                a = d[m[1]]; b = d[m[3]] if m[3] in d else float(m[3])
                ok = a < b if m[2] == '<' else a > b
        print('ok  ' if ok else 'FAIL', c['id'], s)
        bad += not ok
raise SystemExit(1 if bad else 0)
