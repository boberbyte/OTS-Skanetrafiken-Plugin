import importlib.metadata
import os
import pathlib
import traceback

import yaml
from flask import Blueprint, Flask, jsonify, request
from flask import current_app as app
from flask_security import roles_accepted
from opentakserver.extensions import logger
from opentakserver.plugins.Plugin import Plugin

from .default_config import DefaultConfig
from .feeder import Feeder


class SkanetrafikenPlugin(Plugin):
    metadata = pathlib.Path(__file__).resolve().parent.name
    url_prefix = f"/api/plugins/{metadata.lower()}"
    blueprint = Blueprint("SkanetrafikenPlugin", __name__, url_prefix=url_prefix)

    def __init__(self):
        super().__init__()
        self._feeder: Feeder | None = None
        self.load_metadata()

    def activate(self, context: Flask, enabled: bool = True):
        self._app = context
        self._load_config()
        if self._feeder is None:
            self._feeder = Feeder(self._app)
        if enabled:
            self._feeder.start()
            logger.info(f"Successfully Loaded {self.name}")
        else:
            self._feeder.stop()
            logger.info(f"Plugin {self.name} is disabled")

    def stop(self):
        if self._feeder:
            self._feeder.stop()

    def load_metadata(self):
        try:
            self.distro = pathlib.Path(__file__).resolve().parent.name
            self.metadata = importlib.metadata.metadata(self.distro).json
            self.name = self.metadata["name"]
            self.metadata["distro"] = self.distro
            return self.metadata
        except BaseException as e:
            logger.error(e)
            logger.debug(traceback.format_exc())
            return None

    def _load_config(self):
        # Standardvärden först, sedan användarens värden från ~/ots/config.yml.
        with open(os.path.join(self._app.config.get("OTS_DATA_FOLDER"), "config.yml")) as fh:
            on_disk = yaml.safe_load(fh) or {}
        for key in DefaultConfig.keys():
            value = on_disk[key] if key in on_disk else getattr(DefaultConfig, key)
            self._config[key] = value
            self._app.config.update({key: value})

    def get_info(self):
        self.load_metadata()
        self.get_plugin_routes(self.url_prefix)
        return {"name": self.name, "distro": self.distro, "routes": self.routes}

    # Dashboarden anropar /ui; tomt svar = pluginet har inget eget gränssnitt.
    @staticmethod
    @blueprint.route("/ui")
    @roles_accepted("administrator")
    def ui():
        return "", 200

    @staticmethod
    @blueprint.route("/config")
    @roles_accepted("administrator")
    def config():
        return jsonify({key: app.config.get(key) for key in DefaultConfig.keys()})

    @staticmethod
    @blueprint.route("/config", methods=["POST"])
    @roles_accepted("administrator")
    def update_config():
        try:
            result = DefaultConfig.update_config(request.json)
            return jsonify(result), (200 if result["success"] else 400)
        except BaseException as e:
            logger.error("Failed to update config: " + str(e))
            return jsonify({"success": False, "error": str(e)}), 400
