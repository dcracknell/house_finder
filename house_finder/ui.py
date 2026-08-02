"""Local preferences editor: `house-finder ui`.

Serves a small form-based editor for config/profile.json so search criteria can
be changed without hand-editing JSON. Binds to localhost only - it writes to
your config directory and has no authentication.

It can also start a one-off search of an area that is not in your profile,
which is what the "Search another area" button on the map talks to.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from house_finder import PROFILE_PATH, PROJECT_ROOT, SETTINGS_PATH, load_profile

logger = logging.getLogger(__name__)

TEMPLATE_PATH = PROJECT_ROOT / "templates" / "preferences.html"

# Only these settings.yaml keys may be edited from the browser. Anything else
# (paths, SMTP credentials, model pricing) stays a deliberate file edit.
_EDITABLE_SETTINGS = {
    "mode": r"^(active|passive|paused)$",
    "quota_soft_cap_gbp": r"^\d+(\.\d+)?$",
    "stale_listing_days": r"^\d+$",
}


def update_settings_text(text: str, updates: dict) -> str:
    """Apply key updates to settings.yaml as line edits.

    Deliberately not a YAML round-trip: re-dumping would strip every comment in
    the file, and those comments are most of what makes the config readable.
    """
    lines = text.splitlines()
    for key, value in updates.items():
        if key not in _EDITABLE_SETTINGS:
            continue
        if not re.match(_EDITABLE_SETTINGS[key], str(value)):
            logger.warning("ui: rejecting invalid value for %s: %r", key, value)
            continue

        pattern = re.compile(rf"^(\s*){re.escape(key)}\s*:\s*([^#\n]*)(#.*)?$")
        for index, line in enumerate(lines):
            match = pattern.match(line)
            if match:
                indent, _old, comment = match.groups()
                lines[index] = f"{indent}{key}: {value}" + (f" {comment}" if comment else "")
                break
    return "\n".join(lines) + "\n"


def _validate_profile(profile: dict) -> list[str]:
    """Return a list of problems, empty if the profile is usable."""
    problems = []
    modes = profile.get("modes_enabled") or []
    if not modes:
        problems.append("Enable at least one of buy or rent.")

    for mode in modes:
        block = profile.get(mode) or {}
        areas = block.get("search_areas") or []
        if not areas or not any((a.get("postcode_or_place") or "").strip() for a in areas):
            problems.append(f"{mode}: add at least one search area (a postcode or town).")

        price_key = "price_pcm" if mode == "rent" else "price"
        price = block.get(price_key) or {}
        try:
            low = float(price.get("min") or 0)
            high = float(price.get("max") or 0)
        except (TypeError, ValueError):
            problems.append(f"{mode}: prices must be numbers.")
            continue
        if high and low and high < low:
            problems.append(f"{mode}: maximum price is below the minimum.")
        if not high:
            problems.append(f"{mode}: set a maximum price, or every listing will match.")
    return problems


# ---------------------------------------------------------------------------
# One-off area searches
# ---------------------------------------------------------------------------

# dashboard.html is a file on disk, so its requests arrive cross-origin. Only
# local pages are allowed to reach the API: a browser will not let a real
# website claim one of these origins, so no site can make your machine search.
_LOCAL_ORIGIN = re.compile(r"^(null|file://.*|https?://(localhost|127\.0\.0\.1)(:\d+)?)$")

_PLACE = re.compile(r"^[\w ,'&()./-]{1,60}$")

_run_lock = threading.Lock()
_run_state: dict = {
    "running": False,
    "command": "",
    "ok": None,
    "output": "",
    "finished_at": None,
}


def build_run_command(payload: dict) -> list[str]:
    """Turn a request from the map into a `house-finder run` command line."""
    place = str(payload.get("area") or "").strip()
    if not _PLACE.match(place):
        raise ValueError("Enter a postcode or place name.")

    command = [sys.executable, "-m", "house_finder.cli", "run", "--area", place]

    radius = payload.get("radius")
    if radius not in (None, ""):
        try:
            command += ["--radius", str(float(radius))]
        except (TypeError, ValueError) as exc:
            raise ValueError("Radius must be a number.") from exc

    if payload.get("mode") in ("buy", "rent"):
        command += ["--mode", str(payload["mode"])]

    # Scoring costs money, so a curious look at another area is free unless you
    # deliberately ask for the AI pass.
    if not payload.get("use_ai"):
        command += ["--no-rank"]
    return command


def describe_run_command(command: list[str]) -> str:
    """The same command as the user would type it themselves."""
    return " ".join(["house-finder", *command[3:]])


def _run_in_background(command: list[str]) -> None:
    try:
        finished = subprocess.run(
            command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=3600
        )
        output = (finished.stdout or "") + (finished.stderr or "")
        ok = finished.returncode == 0
    except Exception as exc:  # noqa: BLE001 - reported back to the browser
        output, ok = str(exc), False

    with _run_lock:
        _run_state.update(
            running=False, ok=ok, output=output[-4000:], finished_at=time.time()
        )
    logger.info("ui: one-off run finished (ok=%s)", ok)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003 - silence default stderr spam
        logger.debug("ui: " + fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self._allow_local_origin()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _allow_local_origin(self) -> None:
        """Let a dashboard.html opened from disk talk to this server."""
        origin = self.headers.get("Origin")
        if origin and _LOCAL_ORIGIN.match(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self.send_response(204)
        self._allow_local_origin()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _start_run(self) -> None:
        """Start a one-off search for an area that is not in the profile."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError) as exc:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON: {exc}"})
            return

        try:
            command = build_run_command(payload)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        described = describe_run_command(command)
        with _run_lock:
            if _run_state["running"]:
                self._send_json(
                    409, {"ok": False, "error": "A search is already running."}
                )
                return
            _run_state.update(
                running=True, ok=None, output="", finished_at=None, command=described
            )

        logger.info("ui: starting one-off run: %s", described)
        threading.Thread(target=_run_in_background, args=(command,), daemon=True).start()
        self._send_json(202, {"ok": True, "command": described})

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path in ("/", "/index.html"):
            try:
                html = TEMPLATE_PATH.read_text(encoding="utf-8")
            except OSError as exc:
                self._send(500, f"Cannot read editor template: {exc}".encode(), "text/plain")
                return
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if self.path == "/api/run-status":
            with _run_lock:
                self._send_json(200, {"ok": True, "run": dict(_run_state)})
            return

        if self.path == "/api/profile":
            try:
                self._send_json(200, {"ok": True, "profile": load_profile()})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path == "/api/run":
            self._start_run()
            return

        if self.path != "/api/profile":
            self._send(404, b"Not found", "text/plain")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError) as exc:
            self._send_json(400, {"ok": False, "error": f"Invalid JSON: {exc}"})
            return

        profile = payload.get("profile")
        if not isinstance(profile, dict):
            self._send_json(400, {"ok": False, "error": "No profile supplied"})
            return

        problems = _validate_profile(profile)
        if problems:
            self._send_json(400, {"ok": False, "error": " ".join(problems)})
            return

        try:
            PROFILE_PATH.write_text(
                json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            self._send_json(500, {"ok": False, "error": f"Could not write profile: {exc}"})
            return

        settings_updates = payload.get("settings") or {}
        if settings_updates:
            try:
                current = SETTINGS_PATH.read_text(encoding="utf-8")
                SETTINGS_PATH.write_text(
                    update_settings_text(current, settings_updates), encoding="utf-8"
                )
            except OSError as exc:
                self._send_json(
                    500, {"ok": False, "error": f"Profile saved, but settings failed: {exc}"}
                )
                return

        logger.info("ui: saved %s", PROFILE_PATH)
        self._send_json(200, {"ok": True})


def serve(port: int = 8765, open_browser: bool = True) -> None:
    """Run the editor until interrupted."""
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Editor template missing: {TEMPLATE_PATH}")

    server = HTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"

    print(f"\nPreferences editor: {url}")
    print(f"Editing: {PROFILE_PATH}")
    print("Press Ctrl+C to stop.\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
