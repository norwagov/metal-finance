"""Metal Finance - rozliczenia wspólnych wyjazdów ze znajomymi."""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from datetime import timedelta

from flask import Flask, abort, redirect, request, session, url_for

from .models import db, seed_defaults, upgrade_schema
from .money import CURRENCY_SYMBOLS, fmt_amount, fmt_input, fmt_qty

__version__ = "1.0.0"


def _secret_key(data_dir: str) -> str:
    env = os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env
    path = os.path.join(data_dir, "secret_key")
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as fh:
        fh.write(key)
    os.chmod(path, 0o600)
    return key


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    data_dir = os.path.abspath(os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data")))
    if test_config and "DATA_DIR" in test_config:
        data_dir = test_config["DATA_DIR"]
    os.makedirs(os.path.join(data_dir, "uploads"), exist_ok=True)

    app.config.update(
        DATA_DIR=data_dir,
        UPLOAD_DIR=os.path.join(data_dir, "uploads"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL") or f"sqlite:///{os.path.join(data_dir, 'metal.db')}",
        SECRET_KEY=_secret_key(data_dir),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "60")) * 1024 * 1024,
        APP_PASSWORD=os.environ.get("APP_PASSWORD", ""),
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        PERMANENT_SESSION_LIFETIME=timedelta(days=90),
        CSRF_ENABLED=True,
    )
    if test_config:
        app.config.update(test_config)

    if os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes"):
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    db.init_app(app)
    with app.app_context():
        db.create_all()
        upgrade_schema()
        seed_defaults()

    app.jinja_env.filters["money"] = fmt_amount
    app.jinja_env.filters["money_input"] = fmt_input
    app.jinja_env.filters["qty"] = fmt_qty

    @app.template_filter("pldate")
    def pldate(d):
        return d.strftime("%d.%m.%Y") if d else ""

    @app.context_processor
    def inject():
        return {
            "csrf_token": _csrf_token,
            "currency_symbol": lambda c: CURRENCY_SYMBOLS.get(c, c),
            "app_version": __version__,
            "auth_enabled": bool(app.config["APP_PASSWORD"]),
        }

    @app.before_request
    def guard():
        if request.endpoint in ("static", "healthz"):
            return None
        if app.config["APP_PASSWORD"] and not session.get("auth_ok") and request.endpoint != "admin.login":
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("admin.login", next=request.full_path))
        if app.config["CSRF_ENABLED"] and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf", "")
            if not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Sesja wygasła - odśwież stronę i spróbuj ponownie.")
        return None

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    from .views import admin, api, bills, trips

    app.register_blueprint(trips.bp)
    app.register_blueprint(bills.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(api.bp)

    from . import cli

    cli.register(app)
    return app


def _csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]
