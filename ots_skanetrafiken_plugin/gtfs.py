"""Trafiklab/GTFS utan serverberoenden.

Importerar varken Flask eller OpenTAKServer, så att den statiska tidtabellen kan
läsas in i en egen process (python -m ots_skanetrafiken_plugin.gtfs ...). Servern
kör gevent, där ett tungt räknejobb i en tråd skulle stoppa hela servern.
"""
import csv
import gzip
import io
import os
import pickle
import struct
import sys
import time
import urllib.request
import zipfile

RT_URL = "https://opendata.samtrafiken.se/gtfs-rt/{op}/VehiclePositions.pb?key={key}"
STATIC_URL = "https://opendata.samtrafiken.se/gtfs/{op}/{op}.zip?key={key}"


def read_keys(key_file):
    """Nyckelfilen: rubrikrad ('... Realtime' / '... Static ...') följd av en rad med nyckeln."""
    keys, current = {}, None
    with open(os.path.expanduser(key_file)) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            if "realtime" in low:
                current = "realtime"
            elif "static" in low:
                current = "static"
            elif current:
                keys.setdefault(current, line.split()[-1])
                current = None
    return keys


def http_get(url, timeout=30):
    # Trafiklab svarar 406 utan Accept-Encoding: gzip.
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "skanetak"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data


# --- GTFS-realtid: minimal protobuf-avkodare ---------------------------------
# Bara de fält som behövs ur FeedMessage/VehiclePosition. Egen avkodare i stället
# för gtfs-realtime-bindings, som hade tvingat upp protobuf i serverns venv.


def _fields(buf):
    """Ger (fältnummer, trådtyp, värde) för ett protobuf-meddelande."""
    i, n = 0, len(buf)
    while i < n:
        key = shift = 0
        while True:
            b = buf[i]
            i += 1
            key |= (b & 0x7F) << shift
            shift += 7
            if b < 0x80:
                break
        num, wt = key >> 3, key & 7
        if wt == 0:
            val = shift = 0
            while True:
                b = buf[i]
                i += 1
                val |= (b & 0x7F) << shift
                shift += 7
                if b < 0x80:
                    break
        elif wt == 2:
            ln = shift = 0
            while True:
                b = buf[i]
                i += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if b < 0x80:
                    break
            val = buf[i : i + ln]
            if len(val) != ln:
                raise ValueError("avkortat protobuf-meddelande")
            i += ln
        elif wt == 5:
            val = buf[i : i + 4]
            i += 4
        elif wt == 1:
            val = buf[i : i + 8]
            i += 8
        else:
            raise ValueError(f"okänd trådtyp {wt}")
        yield num, wt, val


def _f32(v):
    return struct.unpack("<f", v)[0]


def decode_vehicle_positions(data):
    """FeedMessage -> lista av dict: id, trip_id, lat, lon, bearing, speed, ts (None om saknas)."""
    out = []
    for num, wt, ent in _fields(memoryview(data)):
        if num != 2 or wt != 2:  # FeedMessage.entity
            continue
        ent_id, veh = "", None
        for n, w, v in _fields(ent):
            if n == 1 and w == 2:
                ent_id = bytes(v).decode("utf-8", "replace")
            elif n == 4 and w == 2:  # FeedEntity.vehicle
                veh = v
        if veh is None:
            continue
        d = {"id": ent_id, "trip_id": "", "lat": None, "lon": None, "bearing": None, "speed": None, "ts": 0}
        for n, w, v in _fields(veh):
            if n == 1 and w == 2:  # trip
                for n2, w2, v2 in _fields(v):
                    if n2 == 1 and w2 == 2:
                        d["trip_id"] = bytes(v2).decode("utf-8", "replace")
            elif n == 2 and w == 2:  # position
                for n2, w2, v2 in _fields(v):
                    if w2 != 5:
                        continue
                    if n2 == 1:
                        d["lat"] = _f32(v2)
                    elif n2 == 2:
                        d["lon"] = _f32(v2)
                    elif n2 == 3:
                        d["bearing"] = _f32(v2)
                    elif n2 == 5:
                        d["speed"] = _f32(v2)
            elif n == 5 and w == 0:
                d["ts"] = v
            elif n == 8 and w == 2:  # vehicle descriptor
                for n2, w2, v2 in _fields(v):
                    if n2 == 1 and w2 == 2 and v2:
                        d["id"] = bytes(v2).decode("utf-8", "replace")
        if d["lat"] is not None and d["lon"] is not None:
            out.append(d)
    return out


# --- Statisk tidtabell -------------------------------------------------------


def cache_path(cache_dir, operator):
    return os.path.join(os.path.expanduser(cache_dir), f"{operator}-trips-v2.pickle")


def static_is_fresh(cache_dir, operator, max_age_days):
    cache = cache_path(cache_dir, operator)
    return os.path.exists(cache) and time.time() - os.path.getmtime(cache) < max_age_days * 86400


def build_static(cache_dir, operator, key_file):
    """Bygger trip_id -> (linje, destination, route_type, linjetyp) och sparar som pickle.

    Skånetrafiken lämnar trip_headsign tomt; destinationen står som stop_headsign
    på turens rader i stop_times.txt. En lokal fil <cache>/<operatör>.zip används
    i stället för nedladdning om den finns.
    """
    cache_dir = os.path.expanduser(cache_dir)
    os.makedirs(cache_dir, exist_ok=True)
    cache = cache_path(cache_dir, operator)
    local_zip = os.path.join(cache_dir, f"{operator}.zip")
    if os.path.exists(local_zip):
        zf = zipfile.ZipFile(local_zip)
    else:
        key = read_keys(key_file).get("static")
        if not key:
            raise RuntimeError("ingen statisk nyckel i nyckelfilen")
        zf = zipfile.ZipFile(io.BytesIO(http_get(STATIC_URL.format(op=operator, key=key), timeout=180)))
    routes = {}
    with zf.open("routes.txt") as fh:
        for r in csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig")):
            name = r.get("route_short_name") or r.get("route_long_name") or ""
            routes[r["route_id"]] = (name, int(r.get("route_type") or 3), r.get("route_desc") or "")
    trips = {}
    with zf.open("trips.txt") as fh:
        for t in csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig")):
            name, rtype, desc = routes.get(t["route_id"], ("", 3, ""))
            trips[t["trip_id"]] = (name, t.get("trip_headsign") or "", rtype, desc)
    if "stop_times.txt" in zf.namelist():
        with zf.open("stop_times.txt") as fh:
            rd = csv.reader(io.TextIOWrapper(fh, "utf-8-sig"))
            head = next(rd)
            if "stop_headsign" in head:
                i_trip, i_head = head.index("trip_id"), head.index("stop_headsign")
                for row in rd:
                    cur = trips.get(row[i_trip])
                    if cur and not cur[1] and row[i_head]:
                        trips[row[i_trip]] = (cur[0], row[i_head], cur[2], cur[3])
    with open(cache + ".tmp", "wb") as fh:
        pickle.dump(trips, fh)
    os.replace(cache + ".tmp", cache)
    if os.path.exists(local_zip):
        os.remove(local_zip)
    return len(routes), len(trips)


def load_static(cache_dir, operator):
    cache = cache_path(cache_dir, operator)
    if os.path.exists(cache):
        with open(cache, "rb") as fh:
            return pickle.load(fh)
    return {}


if __name__ == "__main__":  # python -m ots_skanetrafiken_plugin.gtfs <cache> <operatör> <nyckelfil>
    try:
        os.nice(10)
    except OSError:
        pass
    n_routes, n_trips = build_static(sys.argv[1], sys.argv[2], sys.argv[3])
    print(f"{n_routes} linjer, {n_trips} turer")
