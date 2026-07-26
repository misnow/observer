"""Verification for LLM activation-token scoring ($ACTIVE).

The feature: ask a vision LLM to end its reply with a sentinel when some
condition holds ("...if a person is present, end your reply with $ACTIVE").
Seeing it drives the tool's score to 100, which sets its state so a Studio
Vision Wait can gate on it.

The interesting case is the NEGATIVE one. A model asked to emit a sentinel
routinely quotes it back while declining - "no person, so I will not output
$ACTIVE" - and a naive substring test fires on exactly the answer that should
not fire. That is why "Ends with" is the default, and it is asserted here.

Run:
  C:\\Users\\mikes\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tests\\verify_llm_activation_token.py
"""
import os
import sys
import json
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

LOG = os.path.join(tempfile.gettempdir(), "verify_llm_activation_token.log")
_lines = []


def w(*parts):
    _lines.append(" ".join(str(p) for p in parts))


failures = []


def check(label, cond, detail=""):
    w(("  PASS " if cond else "  FAIL ") + label + (f"  {detail}" if detail else ""))
    if not cond:
        failures.append(label)


ISO = tempfile.mkdtemp(prefix="lg_act_")
os.chdir(ISO)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
from PyQt6.QtCore import Qt, QCoreApplication, QSettings  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

_live = QSettings("LightGuide", "Observatory")
SETTINGS_BEFORE = {k: _live.value(k) for k in _live.allKeys()}

import vision_tools  # noqa: E402
import main as om  # noqa: E402

ev = vision_tools.evaluate_activation
TOK = "$ACTIVE"

# =====================================================================
w("=== matcher: positive cases ===")
for text in [
    "Yes, a person is standing near the bench. $ACTIVE",
    "There is a human present.\n$ACTIVE",
    "A person is visible. $ACTIVE.",              # trailing full stop
    "Person detected **$ACTIVE**",                # markdown emphasis
    "Yes. $active",                               # case-insensitive
    "  Yes, someone is there.  $ACTIVE  ",        # surrounding whitespace
]:
    check(f"activates: {text[:44]!r}", ev(text, TOK, "Ends with") is True)

w("")
w("=== matcher: the negative case this design exists for ===")
NEGATIVES = [
    "No person is present, so I will not output $ACTIVE.",
    "I don't see a human. I am withholding the $ACTIVE token.",
    "The room is empty; $ACTIVE would be incorrect here.",
    "If someone were present I would reply $ACTIVE, but nobody is.",
    "There is nobody here, so I cannot say $ACTIVE",
]
for text in NEGATIVES:
    ends = ev(text, TOK, "Ends with")
    contains = ev(text, TOK, "Contains")
    check(f"'Ends with' does NOT activate: {text[:44]!r}", ends is False)
    check("  ...while 'Contains' WOULD have false-positived here", contains is True,
          "which is why Contains is not recommended")

w("")
w("  Note: two of those genuinely END WITH the token - pure suffix matching")
w("  cannot separate them from a real activation, so a negation guard on the")
w("  final clause does it. That guard is a heuristic; 'Exact reply' is the")
w("  mode with no ambiguity to guard against.")

check("negation guard does not misfire on an earlier-sentence 'no'",
      ev("There is no doubt a person is here. $ACTIVE", TOK, "Ends with") is True)
check("negation guard does not misfire on 'not' in a prior sentence",
      ev("The light is not bright. A person is present. $ACTIVE",
         TOK, "Ends with") is True)

w("")
w("=== matcher: 'Exact reply' - the unambiguous mode ===")
check("exact token activates", ev("$ACTIVE", TOK, "Exact reply") is True)
check("exact token with whitespace activates", ev("  $ACTIVE\n", TOK, "Exact reply") is True)
check("exact token with punctuation activates", ev("$ACTIVE.", TOK, "Exact reply") is True)
check("markdown-wrapped exact token activates", ev("**$ACTIVE**", TOK, "Exact reply") is True)
check("the alternative reply does not activate", ev("NONE", TOK, "Exact reply") is False)
for text in NEGATIVES:
    check(f"'Exact reply' immune to: {text[:40]!r}",
          ev(text, TOK, "Exact reply") is False)

w("")
w("=== matcher: token fidelity (a real live finding) ===")
w("  Asked to 'reply with only $ACTIVE', gemini-2.5-flash answered 'ACTIVE' -")
w("  correct judgement, but it dropped the '$'. Models routinely strip such")
w("  characters because they read as markup, so matching is done on the")
w("  token's alphanumeric core rather than the literal string.")
check("token '$ACTIVE' matches a reply of 'ACTIVE' (the observed live case)",
      ev("ACTIVE", TOK, "Exact reply") is True)
check("...and in Ends with mode too",
      ev("Yes, a person is present. ACTIVE", TOK, "Ends with") is True)
check("...and in Contains mode too", ev("I see ACTIVE here", TOK, "Contains") is True)
check("case and markdown still tolerated", ev("**active**", TOK, "Exact reply") is True)
check("word boundaries preserved: '$ACTIVE' does NOT match 'proactive'",
      ev("The system is proactive", TOK, "Ends with") is False)
check("nor does it match 'activent' style run-ons",
      ev("activeness", TOK, "Ends with") is False)
check("the negative reply 'NONE' still does not activate",
      ev("NONE", TOK, "Exact reply") is False)

w("")
w("=== matcher: other behaviour ===")
check("plain negative does not activate", ev("No people are visible.", TOK, "Ends with") is False)
check("empty response does not activate", ev("", TOK, "Ends with") is False)
check("mode Off returns None (not evaluated)", ev("anything $ACTIVE", TOK, "Off") is None)
check("empty token returns None", ev("x $ACTIVE", "", "Ends with") is None)
check("custom token works", ev("all clear PERSON_SEEN", "PERSON_SEEN", "Ends with") is True)
check("'Contains' finds a mid-text token",
      ev("$ACTIVE was observed mid sentence", TOK, "Contains") is True)
check("'Ends with' rejects that same mid-text token",
      ev("$ACTIVE was observed mid sentence", TOK, "Ends with") is False)

# =====================================================================
w("")
w("=== wired into the tool: score + state ===")
om.ObservatoryEngine.launch_observatory = getattr(
    om.ObservatoryEngine, "launch_observatory", lambda self: None)
obs = om.ObservatoryEngine(logger=lambda m: w("   [OBS]", m))
obs.tool_selector.setCurrentText("LLM Vision")
obs.spawn_tool_roi(200, 150)
tool = [i for i in obs.cam_scene.items()
        if getattr(i, 'tool_type', None) == "LLM Vision"][0]
w("   tool:", tool.tool_name, "sensitivity:", tool.sensitivity)

check("default is Off (existing projects behave as before)",
      tool.activation_mode == "Off")
check("default token is $ACTIVE", tool.activation_token == "$ACTIVE")

# Off -> any reply scores 100, the long-standing behaviour
obs.handle_llm_result(tool, "Some descriptive answer.", tool.llm_generation)
check("with scoring Off, any reply still scores 100", tool.current_score == 100,
      str(tool.current_score))

tool.activation_mode = "Ends with"
tool.activation_token = "$ACTIVE"

obs.handle_llm_result(tool, "Yes, a person is in the room. $ACTIVE",
                      tool.llm_generation)
check("activating reply drives score to 100", tool.current_score == 100,
      str(tool.current_score))
check("activation_hit recorded", tool.activation_hit is True)
check("tool STATE set true, so a Studio Vision Wait can gate on it",
      obs.tool_states.get(tool.tool_name) is True,
      str(obs.tool_states.get(tool.tool_name)))

obs.handle_llm_result(tool, "No, the room is empty. I will not output $ACTIVE.",
                      tool.llm_generation)
check("non-activating reply drops score to 0", tool.current_score == 0,
      str(tool.current_score))
check("tool state cleared", obs.tool_states.get(tool.tool_name) is False)
check("the response is still stored for display",
      "room is empty" in obs.tool_responses.get(tool.tool_name, ""))

# a stale worker must still be ignored
tool.activation_hit = True
tool.current_score = 100
obs.handle_llm_result(tool, "stale reply without token", tool.llm_generation + 99)
check("a superseded worker's reply is discarded, not scored",
      tool.current_score == 100, str(tool.current_score))

# =====================================================================
w("")
w("=== properties panel builds and reports ===")
tool.activation_mode = "Ends with"
obs.handle_llm_result(tool, "Person present. $ACTIVE", tool.llm_generation)
obs.load_tool_properties_to_ui(tool)
app.processEvents()
check("panel exposes the activation label", hasattr(obs, 'llm_activation_label'))
label_text = obs.llm_activation_label.text()
w("   label:", label_text)
check("panel reports ACTIVATED", "ACTIVATED" in label_text, label_text[:60])

# =====================================================================
w("")
w("=== persistence (llm_prompt was previously NOT saved at all) ===")
tool.llm_prompt = "Is there a person in this room? If yes, end your reply with $ACTIVE"
tool.activation_mode = "Ends with"
tool.activation_token = "$ACTIVE"

proj = os.path.join(ISO, "act.json")
obs.prompt_save_file = lambda *a, **k: proj
obs.prompt_open_file = lambda *a, **k: proj
obs.save_project()

with open(proj) as f:
    saved = json.load(f)
tool_rows = [t for t in saved.get("tools", []) if t.get("type") == "LLM Vision"]
check("LLM tool serialized", bool(tool_rows))
row = tool_rows[0] if tool_rows else {}
check("llm_prompt is now saved", row.get("llm_prompt", "").startswith("Is there a person"),
      repr(row.get("llm_prompt", ""))[:60])
check("activation_mode saved", row.get("activation_mode") == "Ends with")
check("activation_token saved", row.get("activation_token") == "$ACTIVE")

obs.load_project()
loaded = [i for i in obs.cam_scene.items()
          if getattr(i, 'tool_type', None) == "LLM Vision"]
check("tool reloaded", bool(loaded))
if loaded:
    lt = loaded[0]
    check("prompt survives the round trip",
          lt.llm_prompt.startswith("Is there a person"), repr(lt.llm_prompt)[:60])
    check("activation mode survives", lt.activation_mode == "Ends with")
    check("activation token survives", lt.activation_token == "$ACTIVE")
    # and it still scores correctly after a reload
    obs.handle_llm_result(lt, "Yes, someone is here. $ACTIVE", lt.llm_generation)
    check("reloaded tool still scores 100 on activation", lt.current_score == 100)

w("")
w("=== a project saved before this feature still loads ===")
legacy = {"camera_index": 0, "tools": [{
    "name": "legacy01", "type": "LLM Vision", "x": 10.0, "y": 10.0,
    "w": 250.0, "h": 250.0, "sensitivity": 50, "threshold": 127, "min_area": 100}]}
legacy_path = os.path.join(ISO, "legacy.json")
with open(legacy_path, "w") as f:
    json.dump(legacy, f)
obs.prompt_open_file = lambda *a, **k: legacy_path
obs.load_project()
old = [i for i in obs.cam_scene.items() if getattr(i, 'tool_type', None) == "LLM Vision"]
check("legacy project loads", bool(old))
if old:
    check("legacy tool defaults to Off (behaviour unchanged)",
          old[0].activation_mode == "Off", old[0].activation_mode)
    obs.handle_llm_result(old[0], "anything at all", old[0].llm_generation)
    check("legacy tool still scores 100 on any reply", old[0].current_score == 100)

w("")
w("=== live settings untouched ===")
_after = QSettings("LightGuide", "Observatory")
after = {k: _after.value(k) for k in _after.allKeys()}
changed = [k for k in set(SETTINGS_BEFORE) | set(after)
           if SETTINGS_BEFORE.get(k) != after.get(k)]
check("no live setting written", not changed, f"changed={changed}")

w("")
w("=" * 58)
w("RESULT:", "PASS - all checks green" if not failures
  else f"FAIL - {len(failures)}: {failures}")

with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(_lines) + "\n")

sys.exit(1 if failures else 0)
