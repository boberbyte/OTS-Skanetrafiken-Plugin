import os
import traceback

import yaml
from flask import current_app as app
from opentakserver.extensions import logger

P = "OTS_SKANETRAFIKEN_PLUGIN_"


class DefaultConfig:
    # Laddas först; värden i ~/ots/config.yml går före.
    OTS_SKANETRAFIKEN_PLUGIN_OPERATOR = "skane"
    # Nycklarna ligger i en egen fil och visas därför inte i dashboarden.
    OTS_SKANETRAFIKEN_PLUGIN_KEY_FILE = "~/.trafiklab_key"
    OTS_SKANETRAFIKEN_PLUGIN_CACHE_DIR = "~/.cache/skanetak"
    OTS_SKANETRAFIKEN_PLUGIN_INTERVAL_S = 30
    OTS_SKANETRAFIKEN_PLUGIN_IDLE_CHECK_S = 15
    OTS_SKANETRAFIKEN_PLUGIN_MONTHLY_CAP = 28000
    OTS_SKANETRAFIKEN_PLUGIN_STATIC_MAX_AGE_DAYS = 7
    OTS_SKANETRAFIKEN_PLUGIN_RADIUS_KM = 0  # 0 = ingen gräns (hela operatörens område)
    # Vilka fordonsslag som visas
    OTS_SKANETRAFIKEN_PLUGIN_SHOW_BUS = True
    OTS_SKANETRAFIKEN_PLUGIN_SHOW_TRAIN = True
    OTS_SKANETRAFIKEN_PLUGIN_SHOW_TRAM = True
    OTS_SKANETRAFIKEN_PLUGIN_SHOW_FERRY = True
    OTS_SKANETRAFIKEN_PLUGIN_CENTER = "gps"  # "gps" eller "lat,lon"
    OTS_SKANETRAFIKEN_PLUGIN_FALLBACK_CENTER = "55.609,13.001"  # Malmö C
    OTS_SKANETRAFIKEN_PLUGIN_GROUP = "__ANON__"
    OTS_SKANETRAFIKEN_PLUGIN_ONLY_IN_SERVICE = True

    @staticmethod
    def keys():
        return [k for k in vars(DefaultConfig) if k.startswith(P)]

    @staticmethod
    def validate(config: dict) -> dict:
        try:
            for key, value in config.items():
                if key not in DefaultConfig.keys():
                    return {"success": False, "error": f"{key} is not a valid config key"}
                default = getattr(DefaultConfig, key)
                if isinstance(default, bool):
                    if not isinstance(value, bool):
                        return {"success": False, "error": f"{key} must be true or false"}
                elif isinstance(default, int):
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                        return {"success": False, "error": f"{key} must be a non-negative number"}
                elif not isinstance(value, str) or not value.strip():
                    return {"success": False, "error": f"{key} must be a non-empty string"}
            if config.get(P + "INTERVAL_S", 30) < 10:
                return {"success": False, "error": "INTERVAL_S must be at least 10 (API quota)"}
            if config.get(P + "IDLE_CHECK_S", 15) < 5:
                return {"success": False, "error": "IDLE_CHECK_S must be at least 5"}
            center = config.get(P + "CENTER", "gps")
            for c in (center, config.get(P + "FALLBACK_CENTER", "0,0")):
                if c != "gps":
                    lat, lon = (float(x) for x in c.split(","))
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise ValueError(f"bad position {c}")
            return {"success": True, "error": ""}
        except BaseException as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def update_config(config: dict) -> dict:
        try:
            valid = DefaultConfig.validate(config)
            if not valid["success"]:
                return valid
            path = os.path.join(app.config.get("OTS_DATA_FOLDER"), "config.yml")
            with open(path) as fh:
                on_disk = yaml.safe_load(fh.read())
            for key, value in config.items():
                app.config.update({key: value})
                on_disk[key] = value
            with open(path + ".tmp", "w") as fh:
                yaml.safe_dump(on_disk, fh)
            os.chmod(path + ".tmp", os.stat(path).st_mode & 0o777)
            os.replace(path + ".tmp", path)
            return {"success": True}
        except BaseException as e:
            logger.error(f"Failed to update config: {e}")
            logger.error(traceback.format_exc())
            return {"success": False, "error": str(e)}
