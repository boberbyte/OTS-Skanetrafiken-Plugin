"""Hämtar fordonspositioner och lägger dem hos anslutna TAK-klienter.

Markörerna publiceras direkt på exchange "groups" (routing "<grupp>.OUT"), samma
väg som servern själv använder mot klienterna. De går INTE genom cot_parser:
inget skrivs till databasen och inget skickas vidare till Meshtastic/LoRa.
"""
import json
import math
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

import pika
from opentakserver.extensions import logger

from . import gtfs
from .default_config import P

FEED_UID = "skanetak-feed"
TAG = "Skanetrafiken:"


def dist_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def cot_type(route_type):
    # GTFS: 2/100-199 = tåg, 4/1000-1099 = båt, övrigt (700 buss, 900 spårvagn, 1501 taxi) = fordon
    if route_type == 2 or 100 <= route_type < 200:
        return "a-n-G-E-V-T"
    if route_type == 4 or 1000 <= route_type < 1100:
        return "a-n-S-X"
    return "a-n-G-E-V-C"


def category(route_type):
    """Fordonsslag för filtret i inställningarna: TRAIN, TRAM, FERRY eller BUS (allt övrigt)."""
    if route_type == 2 or 100 <= route_type < 200:
        return "TRAIN"
    if route_type == 0 or 900 <= route_type < 1000:
        return "TRAM"
    if route_type == 4 or 1000 <= route_type < 1100:
        return "FERRY"
    return "BUS"


def make_cot(v, trips, stale_s):
    vid = v["id"]
    name, headsign, rtype, desc = trips.get(v["trip_id"], ("", "", 3, ""))
    callsign = f"{name} {headsign}".strip() if (name or headsign) else f"Fordon {vid[-4:]}"
    now = datetime.now(timezone.utc)
    ts = datetime.fromtimestamp(v["ts"], timezone.utc) if v["ts"] else now
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    track = ""
    if v["bearing"] is not None or v["speed"] is not None:
        track = f'<track course="{v["bearing"] or 0:.0f}" speed="{v["speed"] or 0:.1f}"/>'
    remarks = f"Skånetrafiken {desc or 'fordon'}: {callsign} (fordon {vid})"
    return (
        f'<event version="2.0" uid={quoteattr("SKT-" + vid)} type="{cot_type(rtype)}" how="m-g" '
        f'time="{now.strftime(fmt)}" start="{ts.strftime(fmt)}" '
        f'stale="{(now + timedelta(seconds=stale_s)).strftime(fmt)}">'
        f'<point lat="{v["lat"]:.6f}" lon="{v["lon"]:.6f}" hae="9999999.0" ce="15.0" le="9999999.0"/>'
        f"<detail><contact callsign={quoteattr(callsign)}/>{track}"
        f"<remarks>{escape(remarks)}</remarks></detail></event>"
    )


class Quota:
    """Räknar realtidsanrop per kalendermånad så att Bronze-kvoten inte spräcks."""

    def __init__(self, cache_dir):
        self.path = os.path.join(cache_dir, "quota.json")
        self.month, self.count = "", 0
        try:
            with open(self.path) as fh:
                d = json.load(fh)
                self.month, self.count = d["month"], d["count"]
        except Exception:
            pass
        self._roll()

    def _roll(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m")
        if now != self.month:
            self.month, self.count = now, 0

    def allowed(self, cap):
        self._roll()
        return self.count < cap

    def used(self):
        self.count += 1
        with open(self.path + ".tmp", "w") as fh:
            json.dump({"month": self.month, "count": self.count}, fh)
        os.replace(self.path + ".tmp", self.path)


class Feeder:
    def __init__(self, app):
        self._app = app
        self._stop = None
        self._thread = None
        self._conn = self._ch = None
        self._state = None

    # --- av/på (anropas från pluginets activate/stop) ---
    def start(self):
        if self._thread and self._thread.is_alive() and self._stop and not self._stop.is_set():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True, name="skanetrafiken")
        self._thread.start()

    def stop(self):
        if self._stop:
            self._stop.set()
        self._set_state("disabled")

    # --- hjälp ---
    def cfg(self, key):
        return self._app.config.get(P + key)

    def _cache_dir(self):
        d = os.path.expanduser(self.cfg("CACHE_DIR"))
        os.makedirs(d, exist_ok=True)
        return d

    def _set_state(self, state, **extra):
        """Läget i en liten fil, så att `tak status` och panelikonen kan visa det."""
        if state == self._state and not extra:
            return
        if state != self._state:
            logger.info(f"{TAG} {state}")
        self._state = state
        try:
            path = os.path.join(self._cache_dir(), "status.json")
            with open(path + ".tmp", "w") as fh:
                json.dump({"state": state, "time": int(time.time()), **extra}, fh)
            os.replace(path + ".tmp", path)
        except OSError:
            pass

    def _clients_connected(self):
        ports = (self._app.config.get("OTS_TCP_STREAMING_PORT", 8088), self._app.config.get("OTS_SSL_STREAMING_PORT", 8089))
        flt = " or ".join(f"sport = :{p}" for p in ports)
        try:
            out = subprocess.run(
                ["ss", "-Htn", "state", "established", f"( {flt} )"], capture_output=True, text=True, timeout=5
            ).stdout
            return bool(out.strip())
        except Exception:
            return True

    def _center(self):
        center = self.cfg("CENTER")
        if center == "gps":
            try:
                out = subprocess.run(["gpspipe", "-w", "-n", "12"], capture_output=True, text=True, timeout=15).stdout
                for line in out.splitlines():
                    msg = json.loads(line)
                    if msg.get("class") == "TPV" and msg.get("mode", 0) >= 2:
                        return float(msg["lat"]), float(msg["lon"])
            except Exception as exc:
                logger.warning(f"{TAG} gpsd svarade inte ({exc})")
            center = self.cfg("FALLBACK_CENTER")
        lat, lon = center.split(",")
        return float(lat), float(lon)

    def _refresh_static(self):
        """Bygger om tidtabellscachen i en egen process om den är gammal."""
        cache_dir, op = self._cache_dir(), self.cfg("OPERATOR")
        if not gtfs.static_is_fresh(cache_dir, op, self.cfg("STATIC_MAX_AGE_DAYS")):
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "ots_skanetrafiken_plugin.gtfs", cache_dir, op, self.cfg("KEY_FILE")],
                    capture_output=True, text=True, timeout=900,
                )
                if r.returncode == 0:
                    logger.info(f"{TAG} statisk tidtabell uppdaterad: {r.stdout.strip()}")
                else:  # behåll gammal cache
                    err = (r.stderr.strip().splitlines() or ["okänt fel"])[-1]
                    logger.warning(f"{TAG} kunde inte uppdatera tidtabellen: {err}")
            except Exception as exc:
                logger.warning(f"{TAG} kunde inte uppdatera tidtabellen: {exc}")
        trips = gtfs.load_static(cache_dir, op)
        if not trips:
            logger.warning(f"{TAG} ingen tidtabell - markörerna får bara fordons-id")
        return trips

    def _publish(self, cots, ttl_ms):
        if not self._conn or self._conn.is_closed or not self._ch or self._ch.is_closed:
            c = self._app.config
            self._conn = pika.BlockingConnection(
                pika.ConnectionParameters(
                    c.get("OTS_RABBITMQ_SERVER_ADDRESS", "127.0.0.1"),
                    credentials=pika.PlainCredentials(
                        c.get("OTS_RABBITMQ_USERNAME", "guest"), c.get("OTS_RABBITMQ_PASSWORD", "guest")
                    ),
                    heartbeat=0,
                )
            )
            self._ch = self._conn.channel()
        props = pika.BasicProperties(expiration=str(ttl_ms))
        key = f"{self.cfg('GROUP')}.OUT"
        for cot in cots:
            self._ch.basic_publish(
                exchange="groups", routing_key=key, body=json.dumps({"cot": cot, "uid": FEED_UID}), properties=props
            )

    def _close(self):
        try:
            if self._conn and self._conn.is_open:
                self._conn.close()
        except Exception:
            pass
        self._conn = self._ch = None

    # --- huvudslinga ---
    def _run(self, stop):
        try:
            self._loop(stop)
        except Exception as exc:
            logger.exception(f"{TAG} flödet stannade: {exc}")
            if not stop.is_set():
                self._set_state("error", error=str(exc)[:200])
        finally:
            self._close()

    def _loop(self, stop):
        try:
            key = gtfs.read_keys(self.cfg("KEY_FILE")).get("realtime")
        except OSError as exc:
            key = None
            logger.error(f"{TAG} kan inte läsa nyckelfilen: {exc}")
        if not key:
            self._set_state("error", error="ingen realtidsnyckel i nyckelfilen")
            return
        url = gtfs.RT_URL.format(op=self.cfg("OPERATOR"), key=key)
        quota = Quota(self._cache_dir())
        pause_flag = os.path.join(self._cache_dir(), "paused")
        trips, trips_checked, center, center_src = None, 0.0, None, None
        while not stop.is_set():
            if os.path.exists(pause_flag):
                self._close()
                self._set_state("paused")
                stop.wait(self.cfg("IDLE_CHECK_S"))
                continue
            if not self._clients_connected():
                self._close()
                self._set_state("waiting")  # ingen TAK-klient ansluten: inga anrop
                stop.wait(self.cfg("IDLE_CHECK_S"))
                continue
            if not quota.allowed(self.cfg("MONTHLY_CAP")):
                self._set_state("quota")
                stop.wait(600)
                continue
            if trips is None or time.time() - trips_checked > 6 * 3600:
                trips, trips_checked = self._refresh_static(), time.time()
            interval, radius = self.cfg("INTERVAL_S"), self.cfg("RADIUS_KM")
            # Mittpunkten räknas om när inställningen ändras i dashboarden.
            center_cfg = (self.cfg("CENTER"), self.cfg("FALLBACK_CENTER"))
            if radius > 0 and (center is None or center_cfg != center_src):
                center, center_src = self._center(), center_cfg
            started = time.time()
            try:
                vehicles = gtfs.decode_vehicle_positions(gtfs.http_get(url))
                quota.used()
                cots = []
                for v in vehicles:
                    if self.cfg("ONLY_IN_SERVICE") and not v["trip_id"]:
                        continue
                    if radius > 0 and dist_km(center[0], center[1], v["lat"], v["lon"]) > radius:
                        continue
                    # Fordon utan känd tur räknas som buss.
                    if not self.cfg("SHOW_" + category(trips.get(v["trip_id"], ("", "", 3, ""))[2])):
                        continue
                    cots.append(make_cot(v, trips, stale_s=interval * 3))
                if stop.is_set():
                    break
                self._publish(cots, ttl_ms=interval * 1000)
                self._set_state("running", vehicles=len(cots), calls=quota.count)
            except Exception as exc:
                # Nyckeln står i URL:en - logga aldrig undantag som kan innehålla den.
                msg = str(exc).replace(key, "***")
                logger.warning(f"{TAG} fel vid hämtning: {msg}")
                self._close()
            stop.wait(max(1, interval - (time.time() - started)))
