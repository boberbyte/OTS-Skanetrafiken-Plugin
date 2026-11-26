# OTS-Skanetrafiken-Plugin

An [OpenTAKServer](https://github.com/brian7704/OpenTAKServer) plugin that shows
public transport vehicles in Skåne, Sweden (buses, trains and ferries run by
Skånetrafiken) as live markers in ATAK, iTAK and WinTAK.

Positions come from [Trafiklab](https://www.trafiklab.se/)'s *GTFS Regional
Realtime* feed. Each marker carries the line number and destination, for
example `15 Arlöv Strandängen via Södervärn`, with course and speed.

Other Swedish regions served by GTFS Regional (`sl`, `vt`, `ul` and so on) should
work by changing one setting, but only `skane` has been tested.

## What it does

- Turns on and off from **Plugins** in the OpenTAKServer web UI, with a config tab.
- Only calls the API while at least one TAK client is connected.
- Keeps a monthly cap on API calls, so the free Bronze quota (30 000 realtime
  calls a month) is not exceeded.
- Sends markers straight to the connected clients. They are not stored in the
  server database, do not appear on the web map, and are not forwarded to
  Meshtastic/LoRa.
- No extra dependencies. The realtime feed is read by a small built-in protobuf
  decoder, so the server's own `protobuf` version is left alone.

## Requirements

- OpenTAKServer with plugins enabled (`OTS_ENABLE_PLUGINS: true`). Developed on 1.8.3.
- A free Trafiklab account with two API keys, see
  [Getting API keys from Trafiklab](#getting-api-keys-from-trafiklab).
- Linux with `ss` (iproute2), used to see whether a TAK client is connected.

## Installation

```shell
~/.opentakserver_venv/bin/pip install git+https://github.com/boberbyte/OTS-Skanetrafiken-Plugin.git
sudo systemctl restart opentakserver
```

Then reload the web UI as an administrator. The plugin has its own entry in the
side menu.

## Getting API keys from Trafiklab

The plugin cannot fetch anything without your own API keys. They are free.

1. Create an account at [trafiklab.se](https://www.trafiklab.se/) and log in.
2. Create a project in the developer portal.
3. In the project, add an API key for each of these two APIs:
   - **GTFS Regional Realtime** (vehicle positions)
   - **GTFS Regional Static data** (line numbers and destinations)
4. Put the keys in a file on the server, by default `~/.trafiklab_key`, and
   protect it with `chmod 600 ~/.trafiklab_key`.

The keys are read from that file so that they never show up in the web UI or in
`config.yml`. Format: a heading line containing `Realtime` or `Static`, followed
by a line where the key is the last word.

```
GTFS Regional Realtime
Key: 0123456789abcdef0123456789abcdef

GTFS Regional Static data
Key: fedcba9876543210fedcba9876543210
```

New accounts get the Bronze level: 30 000 realtime calls and 50 static downloads
a month. That is enough for this plugin with the default settings.

Without a static key the plugin still works, but markers show a vehicle id
instead of line and destination.

## Settings

All settings are prefixed `OTS_SKANETRAFIKEN_PLUGIN_` and can be changed in the
plugin's Config tab or in `~/ots/config.yml`.

| Setting | Default | Meaning |
|---|---|---|
| `OPERATOR` | `skane` | Trafiklab operator code |
| `KEY_FILE` | `~/.trafiklab_key` | File with the API keys |
| `CACHE_DIR` | `~/.cache/skanetak` | Timetable cache, call counter, status |
| `INTERVAL_S` | `30` | Seconds between fetches (minimum 10) |
| `IDLE_CHECK_S` | `15` | How often to check for clients while idle |
| `MONTHLY_CAP` | `28000` | Maximum realtime calls per calendar month |
| `STATIC_MAX_AGE_DAYS` | `7` | How often the timetable is downloaded again |
| `RADIUS_KM` | `0` | Only vehicles within this radius of the centre; 0 = whole region |
| `CENTER` | `gps` | `gps` (asks gpsd once) or `lat,lon` |
| `FALLBACK_CENTER` | `55.609,13.001` | Used when there is no GPS fix |
| `GROUP` | `__ANON__` | TAK group that receives the markers |
| `ONLY_IN_SERVICE` | `true` | Skip vehicles that are not on a trip |

At 30 seconds the cap lasts about 230 hours of connected time a month.

## Pausing without the web UI

Creating the file `<CACHE_DIR>/paused` pauses fetching; removing it resumes. The
current state is written to `<CACHE_DIR>/status.json`, which is handy for
scripts and tray icons.

## Notes

- Code comments and log messages are in Swedish.
- The timetable (about 4 MB cached) is parsed in a separate process, because
  OpenTAKServer runs on gevent and heavy work in a thread would stall the server.
- Data from Trafiklab is subject to Trafiklab's own terms.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
