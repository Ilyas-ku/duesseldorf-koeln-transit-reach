"""Trains for the Düsseldorf–Köln map from Deutsche Bahn's own 2025 data
(Bahn-Vorhersage, parsed into bahn-delays-2025/data/events).

Why: in this region the GTFS-RT collector sees only about a third of the
rail stop events DB reports (S-Bahn ~11k a day vs ~34k), and gtfs.de's
regional timetable has no ICE/IC at all, although ICEs link Köln and
Düsseldorf in 22 minutes.

Writes to data/dus/:
  rail2025_delays.parquet  delay statistics per (our) stop_key / line / mode / hour
                           for S-Bahn and regional trains, weekdays 29 Sep - 14 Nov 2025,
                           replacing the collector's rows for these two modes
  fv_connections.parquet   ICE / IC / EC hops inside the box on Tuesday 14 Oct 2025
  fv_stops.parquet         their stations ("db:<EVA>")
  fv_delays.parquet        delay statistics for those trains, same weeks

Same statistics as export_delays.py + export_cost.py: n, cancel_rate (planned
closures excluded: a stop x line x day skipped >= 50% with >= 4 events),
q50 q80 q90 q95, mean q10 q25 q75 of non-cancelled arrival delays.

    python db2025_rail.py
"""
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
EVENTS = (HERE.parents[1] / "bahn-delays-2025" / "data" / "events").as_posix()
NAMES = (HERE.parents[1] / "bahn-delays-2025" / "data" / "stop_names.parquet").as_posix()
OUT = HERE / "data" / "dus"
S, W, N, E = 50.80, 6.50, 51.42, 7.20
FROM, TO, HOLIDAYS = "2025-09-29", "2025-11-14", ("2025-10-03",)
TT_DAY = "2025-10-14"                      # Tuesday, the timetable for ICE / IC
MATCH_M = 300                              # our rail stop -> DB station

con = duckdb.connect()
con.execute("SET threads=8"); con.execute("SET memory_limit='12GB'"); con.execute("SET TimeZone='Europe/Berlin'")
con.execute(f"""
    CREATE TABLE ev AS
    SELECT trip_id, stop_sequence, is_arrival, stop_id, category, line_label, is_regional,
           date_local AS d, hour_local AS hour, ts_local, delay, is_cancelled, lat, lon,
           CASE WHEN category = 'S' THEN 'S-Bahn' WHEN is_regional THEN 'Regional rail' ELSE 'Long distance' END AS mode,
           CASE WHEN is_regional THEN replace(line_label, ' ', '') ELSE category END AS lin
    FROM read_parquet('{EVENTS}/month=*/*.parquet', hive_partitioning = true)
    WHERE month IN (9, 10, 11) AND date_local BETWEEN DATE '{FROM}' AND DATE '{TO}'
      AND lat BETWEEN {S} AND {N} AND lon BETWEEN {W} AND {E}
      AND category NOT IN ('Bus', 'STB') AND category IS NOT NULL""")
print("events in box:", con.execute("SELECT mode, count(*), count(DISTINCT d) FROM ev GROUP BY 1").fetchall())

# planned closures, as in the collector's consolidate.py
con.execute("""
    CREATE TABLE closed AS SELECT d, stop_id, lin FROM ev GROUP BY 1, 2, 3
    HAVING count(*) >= 4 AND avg(is_cancelled::INT) >= 0.5""")
STATS = """count(*) AS n,
    avg(CASE WHEN is_cancelled AND c.d IS NULL THEN 1.0 ELSE 0.0 END) AS cancel_rate,
    quantile_cont(CASE WHEN NOT is_cancelled THEN delay END, 0.5) AS q50,
    quantile_cont(CASE WHEN NOT is_cancelled THEN delay END, 0.8) AS q80,
    quantile_cont(CASE WHEN NOT is_cancelled THEN delay END, 0.9) AS q90,
    quantile_cont(CASE WHEN NOT is_cancelled THEN delay END, 0.95) AS q95,
    avg(CASE WHEN NOT is_cancelled THEN delay END) AS mean,
    quantile_cont(CASE WHEN NOT is_cancelled THEN delay END, 0.10) AS q10,
    quantile_cont(CASE WHEN NOT is_cancelled THEN delay END, 0.25) AS q25,
    quantile_cont(CASE WHEN NOT is_cancelled THEN delay END, 0.75) AS q75"""
hol = ", ".join(f"DATE '{h}'" for h in HOLIDAYS)
st = con.execute(f"""
    SELECT e.stop_id, e.lin AS line, e.mode, e.hour, {STATS}
    FROM ev e LEFT JOIN closed c ON c.d = e.d AND c.stop_id = e.stop_id AND c.lin = e.lin
    WHERE e.is_arrival AND dayofweek(e.d) BETWEEN 1 AND 5 AND e.d NOT IN ({hol})
      AND (e.delay IS NULL OR abs(e.delay) <= 7200)
    GROUP BY 1, 2, 3, 4""").fetchdf()
stations = con.execute(f"""
    SELECT e.stop_id, avg(lat) lat, avg(lon) lon, any_value(n.name) AS name
    FROM ev e LEFT JOIN read_parquet('{NAMES}') n USING (stop_id) GROUP BY 1""").fetchdf()

# --- S-Bahn and regional trains: hand the statistics to our nearest rail stops
ours = pd.read_parquet(OUT / "stops.parquet").drop_duplicates("stop")
conn = pd.read_parquet(OUT / "connections_full.parquet")
rail = set(conn.loc[conn["mode"].isin(["S-Bahn", "Regional rail"]), "arr_stop"])
rs = ours[ours.stop.isin(rail)].drop_duplicates("stop_key").reset_index(drop=True)
kx, ky = np.cos(np.radians(51.1)) * 111320, 110574
dx = (rs.stop_lon.to_numpy()[:, None] - stations.lon.to_numpy()[None, :]) * kx
dy = (rs.stop_lat.to_numpy()[:, None] - stations.lat.to_numpy()[None, :]) * ky
d = np.hypot(dx, dy)
j = d.argmin(axis=1)
rs["eva"] = stations.stop_id.to_numpy()[j]
rs["dist"] = d[np.arange(len(rs)), j]
print(f"our rail stops: {len(rs)}, matched within {MATCH_M} m: {(rs.dist <= MATCH_M).mean():.1%}")
rs = rs[rs.dist <= MATCH_M]
reg = st[st["mode"] != "Long distance"]
rows = rs[["stop_key", "eva"]].merge(reg, left_on="eva", right_on="stop_id").drop(columns=["eva", "stop_id"])
# our lines that the 2025 data knows under the same name
ours_lines = set(conn.loc[conn["mode"].isin(["S-Bahn", "Regional rail"]), "line"])
print("lines in both:", len(ours_lines & set(reg.line)), "only ours:", sorted(ours_lines - set(reg.line))[:20])
rows.to_parquet(OUT / "rail2025_delays.parquet")
print("rail rows:", len(rows), rows.groupby("mode").n.sum().to_dict())

# --- ICE / IC / EC: timetable of one Tuesday, delays of the same weeks
fv = con.execute(f"""
    WITH t AS (
      SELECT trip_id, stop_sequence, stop_id, category,
             max(CASE WHEN is_arrival THEN ts_local END) AS arr,
             max(CASE WHEN NOT is_arrival THEN ts_local END) AS dep
      FROM ev WHERE d = DATE '{TT_DAY}' AND mode = 'Long distance' AND category NOT IN ('FLX', 'THA')
      GROUP BY 1, 2, 3, 4)
    SELECT * FROM (
      SELECT trip_id, category, stop_id AS u, dep,
             lead(stop_id) OVER w AS v, lead(arr) OVER w AS arr
      FROM t WINDOW w AS (PARTITION BY trip_id ORDER BY stop_sequence))
    WHERE v IS NOT NULL AND dep IS NOT NULL AND arr IS NOT NULL""").fetchdf()
day0 = pd.Timestamp(TT_DAY)
sec = lambda s: ((s.dt.tz_localize(None) if s.dt.tz is not None else s) - day0).dt.total_seconds().astype(int)
fc = pd.DataFrame({"trip": "db:" + fv.trip_id.astype(str), "line": fv.category, "mode": "Long distance",
                   "dep_stop": "db:" + fv.u.astype(str), "dep_s": sec(fv.dep),
                   "arr_stop": "db:" + fv.v.astype(str), "arr_s": sec(fv.arr)})
fc = fc[(fc.dep_s >= 3 * 3600) & (fc.arr_s <= 28 * 3600) & (fc.arr_s >= fc.dep_s)]
fc.to_parquet(OUT / "fv_connections.parquet")
used = set(fc.dep_stop) | set(fc.arr_stop)
fs = pd.DataFrame({"stop": "db:" + stations.stop_id.astype(str), "stop_key": "db:" + stations.stop_id.astype(str),
                   "stop_name": stations.name.fillna(""), "stop_lat": stations.lat, "stop_lon": stations.lon})
fs = fs[fs.stop.isin(used)]
fs.to_parquet(OUT / "fv_stops.parquet")
fd = st[st["mode"] == "Long distance"].copy()
fd["stop_key"] = "db:" + fd.stop_id.astype(str)
fd.drop(columns="stop_id").to_parquet(OUT / "fv_delays.parquet")
print(f"long distance: {fc.trip.nunique()} trips, {len(fc)} hops, {len(fs)} stations:",
      ", ".join(sorted(fs.stop_name)[:30]))
print(fc.groupby("line").trip.nunique().to_dict())
