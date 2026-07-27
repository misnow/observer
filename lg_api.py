"""
Client for the **Light Guide Systems Web API** - the other product's HTTP
external-control interface.

Naming: that product is "LightGuide Systems" (LGS) by Light Guide Systems, and
this project happens to share the name. Throughout this module, "LG" always
means the *external* product, never this app.

    this app  ---- HTTP ---->  LGS Web API   (run programs, query the DB,
                                              switch modes, drive the window)

Port 54274 by default, straight from the LGS wiki:

    54274 - Port favored for Light Guide Web API (spells LG API on phones)
    54448 - Port favored by Light Guide for general TCP/IP communications

It is 54274 that serves the Web API. The host is configurable because the LGS
machine is usually a *different* PC on the line, not localhost.

WHAT IS VERIFIED AND WHAT IS NOT
--------------------------------
Every endpoint below carries a `verified` flag, and it is not decoration.
Only two were read directly off a real Postman session against a live LGS:

    POST /Programs/Run?wait=false
    GET  (settings - name seen as "Get Settings", path NOT legible)

The rest are inferred from the request *names* in that collection, so their
paths are a best guess at the URL shape and are marked verified=False. Do not
trust an unverified path in production until `probe()` or the real collection
confirms it - and prefer `load_collection()`, which reads the authoritative
Postman JSON that the LGS wiki tells you to download, so nothing is guessed:

    "To see the full list of options, please download the Postman JSON
     collection for out-of-the-box commands to send to any LGS Web API."

RESPONSE ENVELOPE
-----------------
Observed live, LGS replies with a consistent wrapper:

    {"Verb": "POST",
     "Endpoint": "http://localhost:54274/Programs/Run?wait=false",
     "ReturnType": "Result",
     "ResponseItem": ""}

`LGResponse.item` gives you ResponseItem directly, `.envelope` the whole thing.

SIDE EFFECTS - READ THIS
------------------------
This talks to industrial equipment. `run_program()` starts a work instruction
in front of an operator; `shutdown()` and `restart()` stop the application.
Those three are gated behind an explicit `confirm=True` so no code path can
fire them by accident, and `discover()` only ever issues GETs.
"""

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from PyQt6.QtCore import QThread, pyqtSignal


DEFAULT_HOST = "localhost"
DEFAULT_PORT = 54274          # LG API on a phone keypad
DEFAULT_TIMEOUT = 10.0

# QSettings keys, in the namespace both apps already share.
SETTINGS_PREFIX = "lg_api_"


class LGError(RuntimeError):
    """Any failure talking to the LGS Web API."""


# --------------------------------------------------------------------------
# Endpoint table
# --------------------------------------------------------------------------
# name -> (verb, path, verified, description)
#
# `verified` True means the path itself was read off a live request. False
# means only the request's NAME is known and the path is inferred - correct it
# from the real Postman collection (see load_collection) before relying on it.
ENDPOINTS = {
    "run_program":      ("POST", "/Programs/Run",        True,
                         "Run the loaded work instruction (?wait=true|false)"),
    "get_settings":     ("GET",  "/Settings",            False,
                         "Read the LGS settings ('Get Settings' in Postman)"),
    "database_query":   ("POST", "/Database/Query",      False,
                         "Run a database query - query goes in the BODY, "
                         "not the URI (changed in a 2.x release)"),
    "run_mode":         ("POST", "/RunMode",             False, "Switch to Run Mode"),
    "design_mode":      ("POST", "/DesignMode",          False, "Switch to Design Mode"),
    "shutdown":         ("POST", "/Shutdown",            False, "Shut the LGS app down"),
    "restart":          ("POST", "/Restart",             False, "Restart the LGS app"),
    "minimize":         ("POST", "/Minimize",            False, "Minimise the LGS window"),
    "maximize":         ("POST", "/Maximize",            False, "Maximise the LGS window"),
    "window_default":   ("POST", "/Window/Default",      False, "Window: default layout"),
    "window_normal":    ("POST", "/Window/Normal",       False, "Window: normal layout"),
    "window_focus_hmi": ("POST", "/Window/Focus/HMI",    False, "Window: focus the HMI"),
    "window_focus_canvas": ("POST", "/Window/Focus/Canvas", False,
                            "Window: focus the canvas"),
    "window_custom":    ("POST", "/Window/Custom",       False, "Window: custom layout"),
}

# Actions with real-world consequences. Gated behind confirm=True.
DESTRUCTIVE = {"shutdown", "restart", "run_program"}


class LGConfig:
    """Where the LGS Web API lives. The host is usually another PC."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 timeout=DEFAULT_TIMEOUT, scheme="http"):
        self.host = str(host or DEFAULT_HOST).strip()
        self.port = int(port or DEFAULT_PORT)
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.scheme = scheme

    @property
    def base_url(self):
        return f"{self.scheme}://{self.host}:{self.port}"

    def url_for(self, path):
        return urllib.parse.urljoin(self.base_url + "/", path.lstrip("/"))

    # --- persistence ------------------------------------------------------
    @classmethod
    def from_settings(cls, settings):
        """Load from a QSettings, falling back to the defaults."""
        get = settings.value
        return cls(host=get(SETTINGS_PREFIX + "host", DEFAULT_HOST),
                   port=get(SETTINGS_PREFIX + "port", DEFAULT_PORT),
                   timeout=get(SETTINGS_PREFIX + "timeout", DEFAULT_TIMEOUT))

    def to_settings(self, settings):
        settings.setValue(SETTINGS_PREFIX + "host", self.host)
        settings.setValue(SETTINGS_PREFIX + "port", self.port)
        settings.setValue(SETTINGS_PREFIX + "timeout", self.timeout)

    def __repr__(self):
        return f"LGConfig({self.base_url}, timeout={self.timeout}s)"


class LGResponse:
    """One reply, envelope unwrapped."""

    def __init__(self, ok, status, envelope=None, raw="", error="", url=""):
        self.ok = ok
        self.status = status
        self.envelope = envelope if isinstance(envelope, dict) else {}
        self.raw = raw
        self.error = error
        self.url = url

    @property
    def item(self):
        """ResponseItem, the payload LGS actually returns."""
        return self.envelope.get("ResponseItem", "")

    @property
    def return_type(self):
        return self.envelope.get("ReturnType", "")

    def __bool__(self):
        return bool(self.ok)

    def __repr__(self):
        if self.ok:
            return (f"LGResponse(ok, {self.status}, type={self.return_type!r}, "
                    f"item={str(self.item)[:60]!r})")
        return f"LGResponse(FAILED, {self.status}, {self.error[:80]!r})"


class LGClient:
    """Synchronous client. Use LGCallWorker to keep it off the UI thread."""

    def __init__(self, config=None):
        self.config = config or LGConfig()
        # Populated by load_collection(); overrides ENDPOINTS when present.
        self.collection = {}

    # --- liveness ---------------------------------------------------------
    def ping(self):
        """Is anything listening? A plain TCP connect, so it cannot trigger
        an action - unlike an HTTP request to an endpoint we only guessed."""
        try:
            sock = socket.socket()
            sock.settimeout(min(3.0, self.config.timeout))
            reachable = sock.connect_ex((self.config.host, self.config.port)) == 0
            sock.close()
            return reachable
        except Exception:
            return False

    # --- the one place a request is made ----------------------------------
    def request(self, verb, path, params=None, body=None, headers=None):
        url = self.config.url_for(path)
        if params:
            # Drop None so callers can pass optional params freely.
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)

        data = None
        send_headers = {"Accept": "application/json"}
        if body is not None:
            data = (body if isinstance(body, (bytes, bytearray))
                    else json.dumps(body).encode("utf-8"))
            send_headers["Content-Type"] = "application/json"
        send_headers.update(headers or {})

        req = urllib.request.Request(url, data=data, headers=send_headers,
                                     method=verb.upper())
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                raw = ""
            return LGResponse(False, e.code, _try_json(raw), raw,
                              f"HTTP {e.code}: {raw[:200] or e.reason}", url)
        except urllib.error.URLError as e:
            return LGResponse(False, 0, None, "",
                              f"Could not reach {url} - {e.reason}. "
                              f"Check the host/port and that the LGS Web API "
                              f"service is running.", url)
        except Exception as e:
            return LGResponse(False, 0, None, "", f"{type(e).__name__}: {e}", url)

        return LGResponse(True, status, _try_json(raw), raw, "", url)

    # --- generic dispatch -------------------------------------------------
    def call(self, name, params=None, body=None, confirm=False):
        """Invoke a named endpoint from the table (or a loaded collection)."""
        spec = self.collection.get(name) or ENDPOINTS.get(name)
        if spec is None:
            raise LGError(
                f"Unknown LGS endpoint {name!r}. Known: "
                f"{', '.join(sorted(set(ENDPOINTS) | set(self.collection)))}")
        verb, path = spec[0], spec[1]
        if name in DESTRUCTIVE and not confirm:
            raise LGError(
                f"{name!r} changes what the LGS station is doing "
                f"({spec[3] if len(spec) > 3 else ''}). "
                f"Pass confirm=True to really send it.")
        return self.request(verb, path, params=params, body=body)

    # --- prebuilt calls ---------------------------------------------------
    def get_settings(self):
        """Read LGS settings. Safe: GET only."""
        return self.call("get_settings")

    def run_program(self, wait=False, confirm=False, **params):
        """Run the loaded work instruction.

        wait=False returns as soon as it has started; wait=True blocks until
        the program finishes, so raise the client timeout accordingly.
        """
        return self.call("run_program",
                         params={"wait": str(bool(wait)).lower(), **params},
                         confirm=confirm)

    def database_query(self, query):
        """Run a database query.

        The query goes in the BODY, not the URI - the LGS changelog records
        this switch ("Database Query Endpoint for API now takes query in body
        of request instead of URI string"), so a URI-style call may work
        against older builds only.
        """
        return self.call("database_query", body={"Query": query})

    def set_run_mode(self):
        return self.call("run_mode")

    def set_design_mode(self):
        return self.call("design_mode")

    def shutdown(self, confirm=False):
        return self.call("shutdown", confirm=confirm)

    def restart(self, confirm=False):
        return self.call("restart", confirm=confirm)

    def minimize(self):
        return self.call("minimize")

    def maximize(self):
        return self.call("maximize")

    def focus_hmi(self):
        return self.call("window_focus_hmi")

    def focus_canvas(self):
        return self.call("window_focus_canvas")

    # --- discovery --------------------------------------------------------
    def discover(self, extra_paths=()):
        """GET every candidate path and report what answers.

        GET-only on purpose: probing with POST would start programs and shut
        the station down. A 405 (method not allowed) still tells you the path
        EXISTS, which is exactly what an unverified guess needs to confirm.
        """
        results = {}
        candidates = []
        for name, spec in {**ENDPOINTS, **self.collection}.items():
            candidates.append((name, spec[1]))
        for path in extra_paths:
            candidates.append((path, path))

        for name, path in candidates:
            resp = self.request("GET", path)
            if resp.ok:
                verdict = f"OK {resp.status}"
            elif resp.status == 405:
                verdict = "EXISTS (405 - wrong verb for GET, so the path is real)"
            elif resp.status == 404:
                verdict = "not found (404) - path is wrong"
            elif resp.status:
                verdict = f"HTTP {resp.status}"
            else:
                verdict = f"unreachable - {resp.error[:60]}"
            results[name] = (path, verdict)
        return results

    # --- authoritative endpoint list --------------------------------------
    def load_collection(self, path_or_dict):
        """Load endpoints from the LGS Postman JSON collection.

        This is the way to stop guessing: the wiki ships that collection, and
        it holds the exact verb and URL of every command. Anything found here
        overrides the inferred table above.
        """
        if isinstance(path_or_dict, dict):
            data = path_or_dict
        else:
            with open(path_or_dict, "r", encoding="utf-8") as fh:
                data = json.load(fh)

        found = {}

        def walk(items, prefix=""):
            for entry in items or []:
                name = str(entry.get("name", "")).strip()
                if "item" in entry:                       # a folder
                    walk(entry["item"], f"{prefix}{name}/" if name else prefix)
                    continue
                req = entry.get("request")
                if not isinstance(req, dict):
                    continue
                verb = str(req.get("method", "GET")).upper()
                url = req.get("url")
                raw = url.get("raw", "") if isinstance(url, dict) else str(url or "")
                path = _path_from_raw(raw, url)
                if not path:
                    continue
                key = _slug(prefix + name)
                found[key] = (verb, path, True, f"{prefix}{name} (from collection)")

        walk(data.get("item"))
        self.collection.update(found)
        return found


# --------------------------------------------------------------------------
def _try_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


def _path_from_raw(raw, url_obj=None):
    """Pull the path out of a Postman URL, dropping {{variables}} and host."""
    if isinstance(url_obj, dict) and isinstance(url_obj.get("path"), list):
        segments = [str(s) for s in url_obj["path"] if s and "{{" not in str(s)]
        if segments:
            return "/" + "/".join(segments)
    text = str(raw or "")
    # Strip a leading {{var}} or scheme://host:port
    text = text.split("}}", 1)[-1] if "{{" in text.split("/")[0] else text
    parsed = urllib.parse.urlsplit(text if "://" in text else "//x" + text
                                   if text.startswith("/") else text)
    path = parsed.path or ""
    if not path.startswith("/"):
        path = "/" + path
    return path if path != "/" else ""


def _slug(name):
    out = []
    for ch in str(name).strip().lower():
        out.append(ch if ch.isalnum() else "_")
    slug = "".join(out)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_")


class LGCallWorker(QThread):
    """One LGS call off the UI thread.

    Necessary, not merely polite: the LGS box is usually a different PC, so a
    call crosses the network, and run_program(wait=True) blocks for as long as
    the work instruction takes.
    """
    finished_ok = pyqtSignal(object, str)      # LGResponse, error

    def __init__(self, client, name, params=None, body=None, confirm=False,
                 parent=None):
        super().__init__(parent)
        self.client = client
        self.name = name
        self.params = params
        self.body = body
        self.confirm = confirm

    def run(self):
        try:
            resp = self.client.call(self.name, self.params, self.body, self.confirm)
            self.finished_ok.emit(resp, "" if resp.ok else resp.error)
        except Exception as e:
            self.finished_ok.emit(None, f"{type(e).__name__}: {e}")
