"""Admin and account routes: the handlers that decide what the server shares.

Every handler here except the page, session, setup, login and logout calls
``require_admin`` first; writes also require a same-origin request.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import socket
import time
from http import HTTPStatus
from pathlib import Path

from . import admin as admin_auth
from . import auth as admin_accounts
from . import logs as log_setup
from . import settings as user_settings
from .config import load_config
from .ffmpeg import HWACCEL_CHOICES
from .opensubtitles import Account as OpenSubtitlesAccount, OpenSubtitlesError
from .providers import check_key

log = logging.getLogger("litejelly.admin")


def get_local_ip() -> str:
    """The LAN address to show; connecting a UDP socket sends no packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _cached_shows(app) -> list[dict]:
    """One entry per series, for the admin page's per-show cache reset."""
    seen: dict[str, dict] = {}
    for video in app.library.videos:
        if not video.series_id or video.series_id in seen:
            continue
        seen[video.series_id] = {
            "series_id": video.series_id,
            "title": video.title or video.name,
            "category": video.category,
        }
    return sorted(seen.values(), key=lambda row: row["title"].lower())


def _drive_roots() -> list[dict]:
    """Top level of the directory picker: drive letters on Windows, / elsewhere."""
    if os.name != "nt":
        return [{"name": "/", "path": "/"}]
    roots = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = f"{letter}:\\"
        if os.path.exists(candidate):
            roots.append({"name": candidate, "path": candidate})
    return roots


def _save_and_apply(app, app_dir: Path, previous: dict, updated: dict) -> list[str]:
    """Persist and apply settings together; a failed apply restores the previous file."""
    user_settings.save_overrides(app_dir, updated)
    try:
        new_config, warnings = load_config(app_dir)
        app.apply_config(new_config)
    except BaseException:
        user_settings.save_overrides(app_dir, previous)
        raise
    return warnings


class AdminRoutes:
    """Admin handlers. Each receives the active RequestHandler and query dict."""

    @staticmethod
    def admin_page(h, query):
        # The page itself is public; it decides what to show from the session
        # state, and every endpoint behind it is guarded individually.
        h.serve_static_file(h.app.static_dir / "admin.html")

    @staticmethod
    def admin_session(h, query):
        """What the admin page should show: setup, login, or the settings."""
        app = h.app
        client = h.client_address[0] if h.client_address else ""
        if app.credentials is None and app.credentials_damaged:
            h.send_json({"state": "damaged"})
            return
        if app.credentials is None:
            h.send_json({
                "state": "setup",
                "can_set_up_here": admin_auth.is_loopback(client),
                "min_password_length": admin_accounts.MIN_PASSWORD_LENGTH,
            })
            return

        username = app.sessions.validate(admin_auth.session_token(h.headers))
        if username is None:
            locked = app.throttle.locked_for(client)
            h.send_json({
                "state": "login",
                "locked_seconds": int(locked),
            })
            return
        h.send_json({"state": "ready", "username": username})

    @staticmethod
    def admin_setup(h, query):
        """Create the first admin account. Only from the machine itself.

        Allowing this over the network would be a land grab: whoever reached a
        freshly started server first would own it.
        """
        app = h.app
        client = h.client_address[0] if h.client_address else ""
        if app.credentials is not None:
            h.send_api_error(HTTPStatus.CONFLICT, "An admin account already exists.")
            return
        if app.credentials_damaged:
            h.send_api_error(HTTPStatus.CONFLICT,
                             "The admin account file cannot be read. Run "
                             "'python server.py --reset-admin' on the server, then restart it.")
            return
        if not admin_auth.is_loopback(client):
            log.warning("Refused remote admin setup from %s", client)
            h.send_api_error(
                HTTPStatus.FORBIDDEN,
                "The first admin account must be created on the machine "
                "running LiteJelly.")
            return
        if not admin_auth.same_origin(h.headers, h.headers.get("Host", "")):
            h.send_api_error(HTTPStatus.FORBIDDEN, "Cross-site request refused.")
            return

        body = h.read_json_body()
        if body is None:
            return
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")

        errors = admin_accounts.check_username(username)
        errors += admin_accounts.check_password_strength(password)
        if errors:
            h.send_json({"ok": False, "errors": errors}, status=HTTPStatus.BAD_REQUEST)
            return

        credentials = admin_accounts.Credentials(
            username=username,
            password_hash=admin_accounts.hash_password(password),
            updated_at=time.time(),
        )
        try:
            admin_accounts.save_credentials(h.app.config.app_dir, credentials)
        except OSError as exc:
            h.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR,
                             f"Could not save the account: {exc}")
            return

        app.credentials = credentials
        log.info("Admin account created for %s", username)
        h.start_session(username)

    @staticmethod
    def admin_login(h, query):
        app = h.app
        client = h.client_address[0] if h.client_address else ""
        if app.credentials is None:
            h.send_api_error(HTTPStatus.CONFLICT, "No admin account exists yet.")
            return
        if not admin_auth.same_origin(h.headers, h.headers.get("Host", "")):
            h.send_api_error(HTTPStatus.FORBIDDEN, "Cross-site request refused.")
            return

        locked = app.throttle.locked_for(client)
        if locked > 0:
            h.send_json(
                {"ok": False, "errors": [f"Too many attempts. Try again in "
                                         f"{int(locked // 60) + 1} minutes."]},
                status=HTTPStatus.TOO_MANY_REQUESTS)
            return

        body = h.read_json_body()
        if body is None:
            return
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")

        # Compare both, and always run the hash, so a wrong username is not
        # measurably faster to reject than a wrong password.
        name_ok = hmac.compare_digest(username, app.credentials.username)
        password_ok = admin_accounts.verify_password(password,
                                                     app.credentials.password_hash)
        if not (name_ok and password_ok):
            app.throttle.record_failure(client)
            remaining = app.throttle.remaining_attempts(client)
            log.warning("Failed admin sign-in from %s (%d attempts left)",
                        client, remaining)
            h.send_json({"ok": False, "errors": ["Incorrect username or password."],
                         "remaining_attempts": remaining},
                        status=HTTPStatus.UNAUTHORIZED)
            return

        app.throttle.record_success(client)
        log.info("Admin signed in from %s", client)
        h.start_session(username)

    @staticmethod
    def admin_logout(h, query):
        token = admin_auth.session_token(h.headers)
        h.app.sessions.revoke(token)
        h.send_json({"ok": True}, extra_headers={"Set-Cookie": admin_auth.clear_cookie()})

    @staticmethod
    def admin_password(h, query):
        if not h.require_admin(query, write=True):
            return
        app = h.app
        body = h.read_json_body()
        if body is None:
            return

        current = str(body.get("current_password") or "")
        new_password = str(body.get("new_password") or "")
        username = str(body.get("username") or app.credentials.username).strip()

        if not admin_accounts.verify_password(current, app.credentials.password_hash):
            log.warning("Admin password change refused: current password wrong")
            h.send_json({"ok": False, "errors": ["Current password is incorrect."]},
                        status=HTTPStatus.UNAUTHORIZED)
            return

        errors = admin_accounts.check_username(username)
        errors += admin_accounts.check_password_strength(new_password)
        if errors:
            h.send_json({"ok": False, "errors": errors}, status=HTTPStatus.BAD_REQUEST)
            return

        credentials = admin_accounts.Credentials(
            username=username,
            password_hash=admin_accounts.hash_password(new_password),
            updated_at=time.time(),
        )
        try:
            admin_accounts.save_credentials(app.config.app_dir, credentials)
        except OSError as exc:
            h.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR,
                             f"Could not save the account: {exc}")
            return

        app.credentials = credentials
        # Every other session was authorised by the old password.
        app.sessions.revoke_all()
        log.info("Admin password changed; all sessions signed out")
        h.start_session(username)

    @staticmethod
    def admin_settings_get(h, query):
        if not h.require_admin(query):
            return
        config = h.app.config
        h.send_json({
            "settings": config.to_admin_dict(),
            "overrides": user_settings.load_overrides(config.app_dir),
            "content_types": list(user_settings.CONTENT_TYPES),
            "presets": list(user_settings.PRESETS),
            "hwaccels": list(HWACCEL_CHOICES),
            "restart_required_fields": list(user_settings.RESTART_REQUIRED),
            "settings_file": str(user_settings.settings_path(config.app_dir)),
            "library": h.app.library.status,
            "local_ip": get_local_ip(),
            "metadata": {
                "enabled": h.app.metadata is not None,
                "records": h.app.metadata.cache.count() if h.app.metadata else 0,
                "shows": _cached_shows(h.app),
            },
            "ffmpeg": {
                "available": h.app.tools.available,
                "can_probe": h.app.tools.can_probe,
                "ffmpeg_path": str(h.app.tools.ffmpeg or ""),
                "ffprobe_path": str(h.app.tools.ffprobe or ""),
            },
            "version": __import__("litejelly").__version__,
        })

    @staticmethod
    def admin_settings_post(h, query):
        if not h.require_admin(query, write=True):
            return
        body = h.read_json_body()
        if body is None:
            return

        clean, errors = user_settings.validate(body)
        if errors:
            h.send_json({"ok": False, "errors": errors}, status=HTTPStatus.BAD_REQUEST)
            return
        if not clean:
            h.send_json({"ok": False, "errors": ["Nothing to save"]},
                        status=HTTPStatus.BAD_REQUEST)
            return

        app_dir = h.app.config.app_dir
        existing = user_settings.load_overrides(app_dir)

        # The form posts every field, including ones the user never touched.
        # Compare the restart-required ones against what is actually running so
        # a no-op save neither persists a temporary CLI override nor claims a
        # restart is needed.
        running = {"port": h.app.config.port, "host": h.app.config.host}
        for key, current in running.items():
            if key in clean and clean[key] == current and key not in existing:
                del clean[key]
        needs_restart = user_settings.restart_required(running, clean)

        merged = dict(existing)
        for key, value in clean.items():
            if key == "transcode" and isinstance(existing.get("transcode"), dict):
                combined = dict(existing["transcode"])
                combined.update(value)
                merged["transcode"] = combined
            else:
                merged[key] = value

        try:
            warnings = _save_and_apply(h.app, app_dir, existing, merged)
        except Exception as exc:
            log.exception("Settings were not applied")
            h.send_json({"ok": False, "errors": [
                f"Settings were not applied, and the previous ones are still in use: {exc}"]},
                status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        h.send_json({
            "ok": True,
            "settings": h.app.config.to_admin_dict(),
            "warnings": warnings,
            "restart_required": needs_restart,
        })

    @staticmethod
    def admin_browse(h, query):
        """List subdirectories so the admin page can offer a picker."""
        if not h.require_admin(query):
            return
        raw = query.get("path", [""])[0].strip()
        if not raw:
            roots = _drive_roots()
            h.send_json({"path": "", "parent": None, "entries": roots})
            return

        target = Path(os.path.expandvars(os.path.expanduser(raw)))
        try:
            target = target.resolve(strict=True)
        except OSError:
            h.send_api_error(HTTPStatus.NOT_FOUND, "No such directory")
            return
        if not target.is_dir():
            h.send_api_error(HTTPStatus.BAD_REQUEST, "Not a directory")
            return

        entries = []
        try:
            for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                if child.name.startswith("."):
                    continue
                try:
                    if child.is_dir():
                        entries.append({"name": child.name, "path": str(child)})
                except OSError:
                    continue
        except OSError as exc:
            h.send_api_error(HTTPStatus.FORBIDDEN, f"Cannot list directory: {exc}")
            return

        parent = str(target.parent) if target.parent != target else ""
        h.send_json({"path": str(target), "parent": parent, "entries": entries})

    @staticmethod
    def admin_logs(h, query):
        if not h.require_admin(query):
            return
        config = h.app.config
        try:
            limit = int(query.get("lines", ["300"])[0])
        except (TypeError, ValueError):
            limit = 300
        limit = max(1, min(limit, 2000))
        level = query.get("level", [""])[0]

        path = log_setup.log_path(config.app_dir)
        h.send_json({
            "entries": log_setup.read_entries(config.app_dir, limit, level),
            # Name only. Whoever can reach this page knows where it installed
            # LiteJelly, so the absolute path just puts the host's layout on screen.
            "file": log_setup.LOG_FILE,
            "folder": log_setup.LOG_DIR,
            "exists": path.is_file(),
            "size": log_setup.file_size(),
            "to_file": config.log_to_file,
            "to_console": config.log_to_console,
            "verbosity": config.log_verbosity,
            "verbosity_options": list(log_setup.VERBOSITY),
            "view_levels": list(log_setup.VIEW_LEVELS),
        })

    @staticmethod
    def admin_logs_clear(h, query):
        if not h.require_admin(query, write=True):
            return
        path = log_setup.log_path(h.app.config.app_dir)
        try:
            if path.is_file():
                # Truncate rather than unlink: the handler holds it open.
                with open(path, "w", encoding="utf-8"):
                    pass
        except OSError as exc:
            h.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not clear: {exc}")
            return
        log.info("Log file cleared from the admin page")
        h.send_json({"ok": True})

    @staticmethod
    def admin_settings_export(h, query):
        """Hand back settings.json so an install can be rebuilt after a wipe."""
        if not h.require_admin(query):
            return
        overrides = user_settings.load_overrides(h.app.config.app_dir)
        payload = json.dumps(overrides, indent=2, ensure_ascii=False).encode("utf-8")
        stamp = time.strftime("%Y%m%d")
        h.send_bytes(
            payload,
            content_type="application/json; charset=utf-8",
            cache_control="no-store",
            extra={"Content-Disposition":
                   f'attachment; filename="litejelly-settings-{stamp}.json"'},
        )

    @staticmethod
    def admin_settings_import(h, query):
        """Replace the saved settings with an exported file.

        Credentials are not part of settings.json, so a restore can never
        carry an account from one machine to another.
        """
        if not h.require_admin(query, write=True):
            return
        body = h.read_json_body()
        if body is None:
            return
        incoming = body.get("settings") if isinstance(body.get("settings"), dict) else body

        clean, errors = user_settings.validate(incoming)
        if errors:
            h.send_json({"ok": False, "errors": errors}, status=HTTPStatus.BAD_REQUEST)
            return
        if not clean:
            h.send_json({"ok": False, "errors": ["That file has no settings in it"]},
                        status=HTTPStatus.BAD_REQUEST)
            return

        app_dir = h.app.config.app_dir
        needs_restart = user_settings.restart_required(
            {"port": h.app.config.port, "host": h.app.config.host}, clean)
        try:
            warnings = _save_and_apply(h.app, app_dir,
                                       user_settings.load_overrides(app_dir), clean)
        except Exception as exc:
            log.exception("Restored settings were not applied")
            h.send_json({"ok": False, "errors": [
                f"Settings were not applied, and the previous ones are still in use: {exc}"]},
                status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        log.info("Settings restored from a file on the admin page")
        h.send_json({
            "ok": True,
            "settings": h.app.config.to_admin_dict(),
            "warnings": warnings,
            "restart_required": needs_restart,
        })

    @staticmethod
    def admin_encoder_test(h, query):
        """Try an encoder for real, because being listed proves nothing."""
        if not h.require_admin(query, write=True):
            return
        body = h.read_json_body()
        if body is None:
            return
        choice = str(body.get("hwaccel") or "none").lower()
        if choice not in HWACCEL_CHOICES:
            h.send_api_error(HTTPStatus.BAD_REQUEST, "Unknown encoder option")
            return
        result = h.app.tools.test_encoder(choice if choice != "auto" else "none")
        result["hwaccel"] = choice
        h.send_json(result)

    @staticmethod
    def admin_metadata_clear(h, query):
        if not h.require_admin(query, write=True):
            return
        app = h.app
        if app.metadata is None:
            h.send_api_error(HTTPStatus.SERVICE_UNAVAILABLE, "Online metadata is off")
            return
        removed = app.metadata.cache.clear()
        app.metadata.prune_artwork()
        app.library.request_scan(force=True)
        log.info("Metadata cache cleared from the admin page (%d records)", removed)
        h.send_json({"ok": True, "removed": removed})

    @staticmethod
    def admin_metadata_forget(h, query):
        """Re-ask the providers about one show, rather than the whole library."""
        if not h.require_admin(query, write=True):
            return
        app = h.app
        if app.metadata is None:
            h.send_api_error(HTTPStatus.SERVICE_UNAVAILABLE, "Online metadata is off")
            return
        body = h.read_json_body()
        if body is None:
            return

        series_id = str(body.get("series_id") or "")
        video = next((v for v in app.library.videos if v.series_id == series_id), None)
        if video is None:
            h.send_api_error(HTTPStatus.NOT_FOUND, "No such show")
            return

        removed = app.metadata.forget_series(video.title, video.category == "anime")
        app.metadata.prune_artwork()
        app.library.request_scan(force=True)
        log.info("Metadata forgotten for %s (%d records)", video.title, removed)
        h.send_json({"ok": True, "removed": removed, "title": video.title})

    @staticmethod
    def admin_metadata_test(h, query):
        """Ask TMDb or OMDb whether a key works; a filled-in field proves nothing."""
        if not h.require_admin(query, write=True):
            return
        body = h.read_json_body()
        if body is None:
            return
        provider = str(body.get("provider") or "")
        key = str(body.get("key") or "").strip()
        if provider not in ("tmdb", "omdb"):
            h.send_api_error(HTTPStatus.BAD_REQUEST, "Unknown provider")
            return
        if not key or len(key) > 128 or any(ch.isspace() for ch in key):
            h.send_api_error(HTTPStatus.BAD_REQUEST, "That does not look like an API key")
            return
        h.send_json(check_key(provider, key))

    @staticmethod
    def admin_opensubtitles(h, query):
        """What is configured, never including the password."""
        if not h.require_admin(query):
            return
        h.send_json({"ok": True,
                     "opensubtitles": h.app.opensubtitles.public_status()})

    @staticmethod
    def admin_opensubtitles_save(h, query):
        if not h.require_admin(query, write=True):
            return
        body = h.read_json_body()
        if body is None:
            return
        current = h.app.opensubtitles.account
        # A blank password means "leave the stored one alone", so the admin
        # page never has to hold it in order to change the username.
        account = OpenSubtitlesAccount(
            api_key=str(body.get("api_key") or "").strip() or current.api_key,
            username=str(body.get("username") or "").strip() or current.username,
            password=str(body.get("password") or "") or current.password,
        )
        if body.get("forget"):
            account = OpenSubtitlesAccount()
        try:
            h.app.opensubtitles.set_account(account)
        except OSError as exc:
            h.send_json({"ok": False, "error": f"Could not save: {exc}"},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        log.info("OpenSubtitles account %s",
                 "cleared" if body.get("forget") else "updated")
        h.send_json({"ok": True, "opensubtitles": h.app.opensubtitles.public_status()})

    @staticmethod
    def admin_opensubtitles_test(h, query):
        if not h.require_admin(query, write=True):
            return
        try:
            result = h.app.opensubtitles.sign_in()
        except OpenSubtitlesError as exc:
            h.send_json({"ok": False, "error": str(exc)},
                        status=HTTPStatus.BAD_GATEWAY)
            return
        h.send_json({"ok": True, "username": result.get("username", "")})
