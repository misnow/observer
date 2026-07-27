"""Verification for lg_api.py - the Light Guide Systems Web API client.

The LGS machine is a different PC and is not reachable from here, so urlopen is
stubbed and the real request building, envelope parsing, error handling and
safety gates all run against canned replies. The canned envelope is the exact
shape observed in a live Postman session.

What this CANNOT check is whether an inferred endpoint path is correct on a
real LGS. That is what tests/verify_lg_api_live.py is for, to be run on (or
pointed at) the LGS machine.

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_lg_api.py
"""
import os
import sys
import json
import tempfile
import urllib.error
import urllib.request

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_lg_api.log")
_log = open(LOG, "w", encoding="utf-8", buffering=1)
failures = []


def w(*parts):
    line = " ".join(str(p) for p in parts)
    _log.write(line + "\n")
    _log.flush()


def check(label, cond, detail=""):
    w(("  PASS " if cond else "  FAIL ") + label + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


os.chdir(tempfile.mkdtemp(prefix="lg_api_"))

from PyQt6.QtCore import QSettings  # noqa: E402
import lg_api  # noqa: E402

# The envelope LGS really returns, copied from the live Postman response.
LIVE_ENVELOPE = {
    "Verb": "POST",
    "Endpoint": "http://localhost:54274/Programs/Run?wait=false",
    "ReturnType": "Result",
    "ResponseItem": "",
}

sent = []


class FakeResp:
    def __init__(self, payload, status=200):
        self._data = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    sent.append({"url": req.full_url, "method": req.get_method(),
                 "headers": dict(req.header_items()),
                 "body": req.data.decode() if req.data else None})
    return FakeResp(LIVE_ENVELOPE)


_real = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen

# =====================================================================
w("=== config ===")
cfg = lg_api.LGConfig()
check("default port is 54274 (LG API on a keypad)", cfg.port == 54274, str(cfg.port))
check("default base url", cfg.base_url == "http://localhost:54274", cfg.base_url)
remote = lg_api.LGConfig(host="192.168.1.50", port=54274)
check("host is configurable for a remote LGS box",
      remote.base_url == "http://192.168.1.50:54274", remote.base_url)
check("url_for joins cleanly",
      remote.url_for("/Programs/Run") == "http://192.168.1.50:54274/Programs/Run",
      remote.url_for("/Programs/Run"))
check("url_for tolerates a missing leading slash",
      remote.url_for("Programs/Run") == "http://192.168.1.50:54274/Programs/Run")

# settings round trip, in a THROWAWAY namespace - never the live app config
st = QSettings("LightGuideTest", "LGApiVerify")
for k in st.allKeys():
    st.remove(k)
remote.to_settings(st)
back = lg_api.LGConfig.from_settings(st)
check("config survives a QSettings round trip",
      back.host == "192.168.1.50" and back.port == 54274,
      f"{back.base_url}")
for k in st.allKeys():
    st.remove(k)

# =====================================================================
w("")
w("=== request building ===")
client = lg_api.LGClient(lg_api.LGConfig(host="lgs-station", port=54274))

sent.clear()
resp = client.run_program(wait=False, confirm=True)
req = sent[-1]
w("   ", req["method"], req["url"])
check("run_program targets /Programs/Run", "/Programs/Run" in req["url"])
check("run_program is a POST", req["method"] == "POST")
check("wait=false is sent as a query param", "wait=false" in req["url"], req["url"])

sent.clear()
client.run_program(wait=True, confirm=True)
check("wait=true serialises lowercase (JSON-ish, not Python True)",
      "wait=true" in sent[-1]["url"], sent[-1]["url"])

sent.clear()
client.get_settings()
check("get_settings is a GET", sent[-1]["method"] == "GET", sent[-1]["method"])

sent.clear()
client.database_query("SELECT TOP 1 * FROM Results")
req = sent[-1]
check("database_query sends the query in the BODY, not the URI",
      req["body"] and "SELECT TOP 1" in req["body"]
      and "SELECT" not in req["url"],
      f"body={req['body']} url={req['url']}")
check("database_query sets a JSON content type",
      any(k.lower() == "content-type" and "json" in v.lower()
          for k, v in req["headers"].items()),
      str(sorted(req["headers"])))

# =====================================================================
w("")
w("=== response envelope ===")
resp = client.get_settings()
check("response reports ok", bool(resp) and resp.ok)
check("envelope parsed", resp.envelope.get("Verb") == "POST", str(resp.envelope)[:60])
check("ReturnType exposed", resp.return_type == "Result", resp.return_type)
check("ResponseItem exposed via .item", resp.item == "", repr(resp.item))
check("repr is readable", "LGResponse" in repr(resp), repr(resp))

# =====================================================================
w("")
w("=== SAFETY: destructive calls are gated ===")
for name, call in (("shutdown", lambda: client.shutdown()),
                   ("restart", lambda: client.restart()),
                   ("run_program", lambda: client.run_program())):
    try:
        call()
        check(f"{name}() refuses without confirm=True", False, "it sent the request!")
    except lg_api.LGError as e:
        check(f"{name}() refuses without confirm=True", "confirm=True" in str(e),
              str(e)[:70])

sent.clear()
client.shutdown(confirm=True)
check("shutdown(confirm=True) does send", len(sent) == 1 and "/Shutdown" in sent[-1]["url"])

check("non-destructive calls need no confirm",
      bool(client.minimize()) and bool(client.focus_canvas()))

try:
    client.call("does_not_exist")
    check("unknown endpoint raises", False, "no exception")
except lg_api.LGError as e:
    check("unknown endpoint raises and lists what IS known",
          "Unknown LGS endpoint" in str(e) and "run_program" in str(e), str(e)[:70])

# =====================================================================
w("")
w("=== endpoint table honesty ===")
verified = [n for n, s in lg_api.ENDPOINTS.items() if s[2]]
inferred = [n for n, s in lg_api.ENDPOINTS.items() if not s[2]]
w(f"   verified paths: {verified}")
w(f"   inferred paths: {len(inferred)} -> {inferred}")
check("run_program is marked verified (its path was read off a live request)",
      lg_api.ENDPOINTS["run_program"][2] is True)
check("inferred endpoints are marked verified=False, not silently trusted",
      len(inferred) >= 10 and all(not lg_api.ENDPOINTS[n][2] for n in inferred))
check("every endpoint carries a description",
      all(len(s) == 4 and s[3] for s in lg_api.ENDPOINTS.values()))

# =====================================================================
w("")
w("=== error handling ===")
def erroring_urlopen(req, timeout=None):
    raise urllib.error.URLError("[WinError 10061] connection refused")


urllib.request.urlopen = erroring_urlopen
bad = client.get_settings()
check("unreachable host returns a failed response, does not raise",
      bad.ok is False and bad.status == 0)
check("the error names the URL and suggests what to check",
      "lgs-station" in bad.error and "running" in bad.error, bad.error[:90])


def http_500(req, timeout=None):
    raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {},
                                 __import__("io").BytesIO(b'{"detail":"boom"}'))


urllib.request.urlopen = http_500
err = client.get_settings()
check("HTTP error surfaces the status", err.ok is False and err.status == 500)
check("HTTP error keeps the server's body", "boom" in err.error, err.error[:60])


def bad_json(req, timeout=None):
    class R:
        status = 200

        def read(self):
            return b"not json at all"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    return R()


urllib.request.urlopen = bad_json
nj = client.get_settings()
check("a non-JSON reply is still ok, with raw text kept",
      nj.ok and nj.envelope == {} and "not json" in nj.raw)

urllib.request.urlopen = fake_urlopen

# =====================================================================
w("")
w("=== discover() is GET-only (must never start a program) ===")
sent.clear()
results = client.discover()
methods = {r["method"] for r in sent}
check("discover only ever issues GET", methods == {"GET"}, str(methods))
check("discover covers every known endpoint",
      len(results) == len(lg_api.ENDPOINTS), f"{len(results)} probed")
w(f"   sample verdict: run_program -> {results['run_program']}")


def method_not_allowed(req, timeout=None):
    raise urllib.error.HTTPError(req.full_url, 405, "Method Not Allowed", {},
                                 __import__("io").BytesIO(b""))


urllib.request.urlopen = method_not_allowed
res405 = client.discover()
check("a 405 is reported as proof the path EXISTS",
      "EXISTS" in res405["run_program"][1], res405["run_program"][1])


def not_found(req, timeout=None):
    raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {},
                                 __import__("io").BytesIO(b""))


urllib.request.urlopen = not_found
res404 = client.discover()
check("a 404 is reported as the path being wrong",
      "path is wrong" in res404["run_program"][1], res404["run_program"][1])
urllib.request.urlopen = fake_urlopen

# =====================================================================
w("")
w("=== Postman collection import (the authoritative source) ===")
COLLECTION = {
    "info": {"name": "LightGuide Web API"},
    "item": [
        {"name": "Get Settings",
         "request": {"method": "GET",
                     "url": {"raw": "{{path}}/Settings/Get",
                             "host": ["{{path}}"], "path": ["Settings", "Get"]}}},
        {"name": "Application", "item": [
            {"name": "Run Mode",
             "request": {"method": "POST",
                         "url": {"raw": "{{path}}/Application/RunMode",
                                 "path": ["Application", "RunMode"]}}},
            {"name": "Window Focus Canvas",
             "request": {"method": "POST",
                         "url": {"raw": "{{path}}/Application/Window/FocusCanvas",
                                 "path": ["Application", "Window", "FocusCanvas"]}}},
        ]},
        {"name": "Programs", "item": [
            {"name": "Run",
             "request": {"method": "POST",
                         "url": {"raw": "{{path}}/Programs/Run?wait=false",
                                 "path": ["Programs", "Run"],
                                 "query": [{"key": "wait", "value": "false"}]}}},
        ]},
    ],
}
col_path = os.path.join(os.getcwd(), "lgs_collection.json")
with open(col_path, "w", encoding="utf-8") as fh:
    json.dump(COLLECTION, fh)

fresh = lg_api.LGClient(lg_api.LGConfig(host="lgs-station"))
found = fresh.load_collection(col_path)
w("   imported: " + ", ".join(f"{k}={v[0]} {v[1]}" for k, v in sorted(found.items())))
check("collection import found the nested folder requests", len(found) >= 4,
      f"{len(found)}")
check("folder names are included in the key",
      any("programs_run" == k for k in found), str(sorted(found)))
check("the {{path}} variable is stripped from the path",
      all("{{" not in v[1] for v in found.values()), str(sorted(found.values())))
check("imported paths start with /", all(v[1].startswith("/") for v in found.values()))
check("everything imported is marked verified", all(v[2] for v in found.values()))

sent.clear()
fresh.call("programs_run", params={"wait": "false"}, confirm=True)
check("an imported endpoint is callable and OVERRIDES the inferred table",
      "/Programs/Run" in sent[-1]["url"] and "wait=false" in sent[-1]["url"],
      sent[-1]["url"])

sent.clear()
fresh.call("application_window_focus_canvas")
check("a nested imported path is used verbatim",
      "/Application/Window/FocusCanvas" in sent[-1]["url"], sent[-1]["url"])
w("   note: that real path differs from the inferred "
  f"{lg_api.ENDPOINTS['window_focus_canvas'][1]!r} - exactly why the table is "
  "marked unverified and the collection wins.")

# =====================================================================
w("")
w("=== ping() cannot cause side effects ===")
sent.clear()
client.ping()   # TCP connect only
check("ping issues no HTTP request at all", not sent, f"{len(sent)} request(s)")

urllib.request.urlopen = _real
w("")
w("=" * 58)
w("RESULT:", "PASS - all checks green" if not failures
  else f"FAIL - {len(failures)}: {failures}")
_log.flush()
sys.exit(1 if failures else 0)
