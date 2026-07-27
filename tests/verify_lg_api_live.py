"""LIVE probe of a real Light Guide Systems Web API. Run this ON or POINTED AT
the LGS machine - it is the only thing that can confirm the inferred endpoint
paths in lg_api.ENDPOINTS.

    python tests\\verify_lg_api_live.py                 # localhost:54274
    python tests\\verify_lg_api_live.py 192.168.1.50    # remote LGS box
    python tests\\verify_lg_api_live.py 192.168.1.50 54274 C:\\path\\collection.json

SAFE BY DEFAULT. It only ever issues GET requests, so it cannot start a work
instruction or shut the station down. A 405 answer still proves a path exists,
which is all that is needed to confirm a guess.

Nothing here runs a program. To actually drive the station, use lg_api from
your own code with confirm=True, deliberately.
"""
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

import lg_api  # noqa: E402

host = sys.argv[1] if len(sys.argv) > 1 else lg_api.DEFAULT_HOST
port = int(sys.argv[2]) if len(sys.argv) > 2 else lg_api.DEFAULT_PORT
collection = sys.argv[3] if len(sys.argv) > 3 else ""

client = lg_api.LGClient(lg_api.LGConfig(host=host, port=port, timeout=6))
print(f"=== LGS Web API probe: {client.config.base_url} ===\n")

if not client.ping():
    print(f"  Nothing is listening on {host}:{port}.")
    print()
    print("  Checks, in order:")
    print("   1. Is the LGS application running, with its Web API service enabled?")
    print("      The wiki notes the Web API has its own settings file declaring")
    print("      where the self-hosting API lives.")
    print("   2. Is 54274 the right port? That is the LGS default")
    print("      (it spells LG API on a phone keypad). 54448 is LGS's general")
    print("      TCP/IP port and does NOT serve the Web API.")
    print("   3. From another PC, is the Windows firewall allowing 54274 inbound")
    print("      on the LGS machine?")
    sys.exit(1)

print(f"  TCP connect to {host}:{port} succeeded.\n")

if collection:
    try:
        found = client.load_collection(collection)
        print(f"  Loaded {len(found)} endpoint(s) from the Postman collection - "
              f"these are authoritative and override the inferred table.\n")
    except Exception as e:
        print(f"  Could not read the collection ({type(e).__name__}: {e}); "
              f"falling back to the built-in table.\n")

print("  Probing (GET only - nothing here can start or stop anything):\n")
results = client.discover()

real, wrong, unknown = [], [], []
width = max(len(n) for n in results)
for name in sorted(results):
    path, verdict = results[name]
    print(f"    {name:<{width}}  {path:<28} {verdict}")
    if verdict.startswith(("OK", "EXISTS")):
        real.append(name)
    elif "path is wrong" in verdict:
        wrong.append(name)
    else:
        unknown.append(name)

print()
print(f"  confirmed real : {len(real)}")
print(f"  wrong path     : {len(wrong)}  {wrong}")
print(f"  inconclusive   : {len(unknown)}  {unknown}")
print()
if wrong:
    print("  Fix the wrong ones by exporting the LGS Postman collection")
    print("  (wiki: 'download the Postman JSON collection for out-of-the-box")
    print("  commands') and re-running with its path as argument 3. Whatever")
    print("  the collection says wins over the inferred table in lg_api.py.")
sys.exit(0 if not wrong else 1)
