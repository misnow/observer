import sys, json
result_path = r"C:\Users\mikes\AppData\Local\Temp\claude\C--Users-mikes-PycharmProjects-lightproject\2b94bdb8-39d7-4575-a146-dbe9b5a960ad\scratchpad\result_user_wait_vision_gate.txt"
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

# Build a single step with BOTH a Wait for Vision node AND a User Wait node,
# exactly matching the user's authtest1.json + added User Wait scenario.
authoring.new_file()
authoring.step_tree.setCurrentItem(authoring.step_tree.topLevelItem(0))

vw = studio.VisionWaitData()
vw.observatory_file = "dummy.json"
vw.vision_target = "tool01_Moti"
vw.condition = "Is True"
vw_item_tree = studio.QTreeWidgetItem(authoring.step_tree.topLevelItem(0), ["", "Wait for Vision", "", ""])
vw_item_tree.setData(1, studio.Qt.ItemDataRole.UserRole, vw)

authoring.insert_user_wait()
uw = authoring.step_tree.topLevelItem(0).child(1).data(1, studio.Qt.ItemDataRole.UserRole)
assert isinstance(uw, studio.UserWaitData)

authoring.is_running = True
authoring.execution_sequence = authoring.build_execution_sequence()
authoring.pc = 0
authoring.wait_state = "IDLE"
authoring.vision_tool_states = {}
authoring.engine_prev_vision_states = {}

# --- Tick through IDLE -> WAITING_CONFIRMATION with vision condition UNMET ---
authoring._process_execution_engine_body()
w("After IDLE tick: wait_state=", authoring.wait_state)
authoring._process_execution_engine_body()
w("Vision condition NOT met yet. user_wait_conditions_met=", authoring.user_wait_conditions_met,
  "back btn enabled=", authoring.btn_user_wait_back.isEnabled(),
  "fwd btn enabled=", authoring.btn_user_wait_forward.isEnabled())
assert authoring.user_wait_conditions_met is False
assert authoring.btn_user_wait_forward.isEnabled() is False

# Clicking Forward while vision condition is unmet must be a no-op.
before_pc = authoring.pc
authoring._handle_user_wait_button("forward")
w("Clicked forward with vision UNMET -> pc unchanged?", authoring.pc == before_pc)
assert authoring.pc == before_pc

# Run several more ticks - still should not auto-advance (vision unmet).
for _ in range(5):
    authoring._process_execution_engine_body()
w("After 5 more ticks (vision still unmet): pc=", authoring.pc)
assert authoring.pc == 0

# --- NOW satisfy the vision condition ---
authoring.vision_tool_states = {"tool01_Moti": True}
authoring._process_execution_engine_body()
w("After vision condition becomes True: user_wait_conditions_met=", authoring.user_wait_conditions_met,
  "fwd btn enabled=", authoring.btn_user_wait_forward.isEnabled(), "pc=", authoring.pc)
assert authoring.user_wait_conditions_met is True
assert authoring.btn_user_wait_forward.isEnabled() is True
# Must NOT have auto-advanced just because vision condition is now met -
# still waiting on the button click.
assert authoring.pc == 0
assert authoring.wait_state == "WAITING_CONFIRMATION"

# --- NOW click forward -> should actually advance ---
authoring._handle_user_wait_button("forward")
w("After clicking forward (vision met + click): pc=", authoring.pc, "wait_state=", authoring.wait_state)
assert authoring.pc == 1
assert authoring.wait_state == "IDLE"

w("ALL USER-WAIT + VISION-GATE CHECKS PASSED")
log.close()
sys.exit(0)
