import sys
result_path = r"C:\Users\mikes\AppData\Local\Temp\claude\C--Users-mikes-PycharmProjects-lightproject\2b94bdb8-39d7-4575-a146-dbe9b5a960ad\scratchpad\result_run_mode_ui.txt"
log = open(result_path, "w", encoding="utf-8")
def w(*a):
    log.write(" ".join(str(x) for x in a) + "\n")
    log.flush()

sys.path.insert(0, r"C:\Users\mikes\PycharmProjects\lightproject")
from PyQt6.QtWidgets import QApplication
app = QApplication(sys.argv)
import studio

studio.AuthoringInterface.launch_observatory = lambda self: None
canvas = studio.ProjectorCanvas()
authoring = studio.AuthoringInterface(canvas)

def visible(w_): return not w_.isHidden()

# --- Design Mode: insert tools/AI visible, run mirror hidden ---
authoring.enter_design_mode()
w("Design Mode: insert_tools visible=", visible(authoring.insert_tools_container),
  "ai_group visible=", visible(authoring.ai_group),
  "btn_del_line visible=", visible(authoring.btn_del_line),
  "run_sequence_view visible=", visible(authoring.run_sequence_view))
assert visible(authoring.insert_tools_container) is True
assert visible(authoring.ai_group) is True
assert visible(authoring.btn_del_line) is True
assert visible(authoring.run_sequence_view) is False

# Build a 3-step sequence with distinct content for mirror verification.
authoring.new_file()
for _ in range(2):
    authoring.add_new_step_node()
authoring.step_tree.setCurrentItem(authoring.step_tree.topLevelItem(0))
t0 = studio.InteractiveTextItem("Step Zero Text")
authoring.insert_asset(t0, "Text")
authoring.step_tree.setCurrentItem(authoring.step_tree.topLevelItem(1))
t1 = studio.InteractiveTextItem("Step One Text")
authoring.insert_asset(t1, "Text")

# --- Run Mode: insert tools/AI hidden, run mirror visible + populated ---
authoring.enter_run_mode()
w("Run Mode: insert_tools visible=", visible(authoring.insert_tools_container),
  "ai_group visible=", visible(authoring.ai_group),
  "btn_del_line visible=", visible(authoring.btn_del_line),
  "run_sequence_view visible=", visible(authoring.run_sequence_view))
assert visible(authoring.insert_tools_container) is False
assert visible(authoring.ai_group) is False
assert visible(authoring.btn_del_line) is False
assert visible(authoring.run_sequence_view) is True

w("Mirror top-level count:", authoring.run_sequence_view.topLevelItemCount())
assert authoring.run_sequence_view.topLevelItemCount() == 3
# Verify the mirror's text actually matches the real step_tree's text.
for i in range(3):
    real_text = authoring.step_tree.topLevelItem(i).text(1)
    mirror_text = authoring.run_sequence_view.topLevelItem(i).text(1)
    w(f"  step {i}: real='{real_text}' mirror='{mirror_text}'")
    assert real_text == mirror_text

# --- Highlighting: step 0 highlighted initially (pc=0) ---
authoring.pc = 0
authoring.highlight_current_run_step()
from PyQt6.QtGui import QColor
def fg(item, col=0):
    return item.foreground(col).color().name()

HIGHLIGHT = QColor("#ffeb3b").name()
NORMAL = QColor("#a9b7c6").name()

top0 = authoring.run_sequence_view.topLevelItem(0)
top1 = authoring.run_sequence_view.topLevelItem(1)
top2 = authoring.run_sequence_view.topLevelItem(2)
w("pc=0: top0 fg=", fg(top0), "top1 fg=", fg(top1))
assert fg(top0) == HIGHLIGHT
assert fg(top1) == NORMAL
assert top0.font(0).bold() is True
assert top1.font(0).bold() is False

# Step 0's child (the text asset row) should ALSO be highlighted (whole step, not just header row)
child0 = top0.child(0)
w("Step 0 child fg:", fg(child0))
assert fg(child0) == HIGHLIGHT

# --- Advance pc to 1, re-highlight -> step 1 highlighted, step 0 cleared ---
authoring.pc = 1
authoring.highlight_current_run_step()
w("pc=1: top0 fg=", fg(top0), "top1 fg=", fg(top1), "top2 fg=", fg(top2))
assert fg(top0) == NORMAL
assert fg(top1) == HIGHLIGHT
assert fg(top2) == NORMAL
assert top1.font(0).bold() is True
assert top0.font(0).bold() is False

# --- Real engine tick integration: run through and check highlight follows pc ---
authoring.is_running = True
authoring.execution_sequence = authoring.build_execution_sequence()
authoring.pc = 0
authoring.wait_state = "IDLE"
authoring._process_execution_engine_body()  # IDLE tick for step 0
authoring._process_execution_engine_body()  # -> WAITING_CONFIRMATION, no gating -> should auto-advance next tick
authoring._process_execution_engine_body()
w("After a couple of real engine ticks: pc=", authoring.pc,
  "top(pc) highlighted=", fg(authoring.run_sequence_view.topLevelItem(authoring.pc)) == HIGHLIGHT)
assert fg(authoring.run_sequence_view.topLevelItem(authoring.pc)) == HIGHLIGHT

# --- Back to Design Mode restores everything ---
authoring.enter_design_mode()
w("Back to Design Mode: insert_tools visible=", visible(authoring.insert_tools_container),
  "run_sequence_view visible=", visible(authoring.run_sequence_view))
assert visible(authoring.insert_tools_container) is True
assert visible(authoring.run_sequence_view) is False

w("ALL RUN MODE UI CHECKS PASSED")
log.close()
sys.exit(0)
