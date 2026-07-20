from __future__ import annotations

from datetime import timedelta

from flask import Flask

from router_panel.core import ensure_auth_config, ensure_secret_key
from router_panel.web import register_routes


app = Flask(__name__)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

ensure_auth_config()
app.secret_key = ensure_secret_key()
register_routes(app)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
