import sys
import os
import time
import json
import subprocess

from PyQt6.QtWidgets import *
from PyQt6.QtCore import QTimer, Qt, QSettings, QCoreApplication, QPointF
from PyQt6.QtGui import QColor, QBrush, QFont
from PyQt6.QtMultimedia import QMediaPlayer

# Custom components imports
from ui_components import DARK_THEME, LLMWorker, LLMProviderSettingsWidget
# Canvas item classes live in canvas_items.py. AnimatableMixin and
# MediaResizeHandle aren't referenced below, but are re-exported here because
# studio has always been the module these names were reached through.
from canvas_items import (TriggerSettings, AnimatableMixin, InteractiveTextItem,
                          MediaResizeHandle, InteractiveMediaItem, InteractiveShapeItem,
                          Interactive3DModelItem, InteractiveCaptureItem,
                          InteractiveHTMLItem, InteractiveLLMTextItem)

# ---------------------------------------------------------
# 1. DATA STRUCTURES & SEQUENCE NODES
# ---------------------------------------------------------
class FlowControlData:
    def __init__(self, action="End Program", target_step=1):
        self.action = action
        self.target_step = target_step

class TimerData:
    def __init__(self, duration=5.0):
        self.duration = duration

class ClearCanvasData:
    def __init__(self):
        # -1 means "every canvas". With multiple outputs you usually want to
        # blank one screen without disturbing what the others are showing,
        # so the target is selectable rather than always global.
        self.output_canvas = -1

class ObservatoryNodeData:
    def __init__(self, filepath=""):
        self.filepath = filepath

class VisionWaitData:
    def __init__(self):
        self.observatory_file = ""
        self.vision_target = "None"
        self.condition = "Is True"

class UserWaitData:
    """Gates a step on an explicit user click of one of the two large
    Back/Forward buttons shown above the Run Mode output panel, instead of
    (or alongside) automatic vision/timer conditions. Each button has its
    own independently configurable action."""

    def __init__(self):
        self.forward_action = "Next Step"       # Next Step | Previous Step | Jump to Step Line
        self.forward_target_step = 1
        self.backward_action = "Previous Step"  # Next Step | Previous Step | Jump to Step Line
        self.backward_target_step = 1

# TriggerSettings moved to canvas_items.py (imported above) - only the canvas
# item classes ever construct one.

# ---------------------------------------------------------
# CUSTOM TREE WIDGET (Fixes Drag & Drop Numbering)
# ---------------------------------------------------------
class SequenceTreeWidget(QTreeWidget):
    def __init__(self, parent_interface):
        super().__init__()
        self.parent_interface = parent_interface
        self.setHeaderLabels(["#", "Sequence Pipeline", "Action / Trigger", "Properties"])
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def dropEvent(self, event):
        # Enforce exactly two levels: Step Block, then its steps - never deeper.
        # Qt's default tree drop lets you drop an item directly ON another item,
        # nesting it as a child, which would let a step line get nested under
        # another step line, or a Step Block get nested under another Step Block.
        sel = self.selectedItems()
        if not sel:
            event.ignore()
            return
        dragged = sel[0]
        dragged_is_top_level = dragged.parent() is None

        target = self.itemAt(event.position().toPoint())
        drop_pos = self.dropIndicatorPosition()

        if target is None:
            # Empty space below the last row: only valid for reordering Step Blocks.
            valid = dragged_is_top_level
        elif drop_pos == QAbstractItemView.DropIndicatorPosition.OnItem:
            # Dropping directly on an item would nest as its child - never allowed.
            valid = False
        else:
            # Dropping above/below a row makes the dragged item a sibling of it,
            # which only keeps the structure flat if both are at the same level.
            target_is_top_level = target.parent() is None
            valid = (dragged_is_top_level == target_is_top_level)

        if not valid:
            event.ignore()
            return

        super().dropEvent(event)
        QTimer.singleShot(10, self.parent_interface.safe_slot(self.parent_interface.renumber_sequence))

# ---------------------------------------------------------
# 2. CANVAS ASSETS -> canvas_items.py (imported at top)
# ---------------------------------------------------------

# ---------------------------------------------------------
# 3. THE PROJECTOR CANVAS
# ---------------------------------------------------------
class ProjectorCanvas(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Output Canvas")
        screens = QApplication.screens()

        if len(screens) > 1:
            self.setGeometry(screens[1].geometry())
            self.showFullScreen()
        else:
            self.setGeometry(50, 50, 800, 600)

        self.setStyleSheet("background-color: black;")
        self.scene = QGraphicsScene()
        self.scene.setBackgroundBrush(QBrush(QColor("black")))
        self.scene.setSceneRect(0, 0, 1920, 1080)
        self.view = QGraphicsView(self.scene)
        self.view.setStyleSheet("border: none; background-color: black;")
        self.setCentralWidget(self.view)

    def clear_canvas(self):
        self.scene.clear()

# ---------------------------------------------------------
# 4. AUTHORING STUDIO (Execution Workspace)
# ---------------------------------------------------------
class StudioCanvasView(QGraphicsView):
    def __init__(self, scene, parent_interface):
        super().__init__(scene)
        self.parent_interface = parent_interface
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                ext = url.toLocalFile().lower().split('.')[-1]
                t = "Video" if ext in ['mp4', 'avi', 'mkv'] else "Audio" if ext in ['mp3', 'wav'] else "Image"
                self.parent_interface.insert_asset(InteractiveMediaItem(url.toLocalFile(), t), f"Media [{t}]")
            event.acceptProposedAction()

class RunPreviewView(QGraphicsView):
    """Read-only, scaled-down live mirror of the Output Canvas, shown in the
    Sequence Pipeline's spot while in Run Mode. Shares the same QGraphicsScene
    as the actual Output Canvas, so it always shows exactly the same content -
    just fit to whatever size this panel happens to be."""

    def __init__(self, scene):
        super().__init__(scene)
        self.setInteractive(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("background-color: black; border: none;")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

class AuthoringInterface(QMainWindow):
    def __init__(self, canvas):
        super().__init__()
        self.setWindowTitle("LightGuide Studio - Authoring Environment")
        self.setGeometry(200, 200, 1500, 850)
        # canvases[0] is the primary output canvas and always exists;
        # additional canvases are created on demand by the Canvases dropdown.
        # self.canvas / self.studio_scene stay pointed at canvas 0 so all the
        # existing single-canvas paths (authoring view, run preview, item
        # insertion) keep working unchanged - assets only land on another
        # canvas when explicitly assigned one via their Output Canvas
        # property.
        self.canvases = [canvas]
        self.canvas, self.studio_scene = canvas, canvas.scene
        self.settings = QSettings("LightGuide", "Observatory")
        self.active_item = None
        self.is_running = False
        self.in_design_mode = True
        self.execution_sequence = []
        self.pc = 0
        self.wait_state = "IDLE"
        self.user_wait_conditions_met = False
        self.timer_start = 0
        self.timer_duration = 0
        self.last_commanded_file = None
        self.vision_tool_states = {"None": True}
        self.vision_tool_responses = {}
        self.vision_tool_response_times = {}
        self.vision_captures = {}
        self.engine_prev_vision_states = {"None": True}
        self.last_stall_log = 0

        self.setup_ui()
        self.step_tree.itemSelectionChanged.connect(self.handle_tree_selection)
        self.step_tree.itemDoubleClicked.connect(self.handle_tree_double_click)
        self.studio_scene.selectionChanged.connect(self.handle_canvas_selection)

        self.blink_state = True
        self.master_clock = QTimer()
        self.master_clock.timeout.connect(self.process_execution_engine)
        self.master_clock.start(100)

        self.ipc_timer = QTimer()
        self.ipc_timer.timeout.connect(self.read_live_vision_state)
        self.ipc_timer.start(50)

        self.enter_design_mode()
        # Opt-in, not automatic: auto-launching Observatory at startup
        # surprised the user and is only wanted sometimes. The setting
        # persists, and the "Launch Observatory" button is always there
        # for launching on demand.
        if self.settings.value("auto_launch_observatory", False, type=bool):
            QTimer.singleShot(500, self.safe_slot(self.launch_observatory))

    def _write_command_atomic(self, command):
        """Write studio_command.json without a torn-read window.

        Observatory polls this file every 500ms. A plain open(...,"w")
        truncates first, so a poll landing in that window reads an empty
        file. Temp-file + os.replace makes the swap atomic.
        """
        tmp = "studio_command.json.tmp"
        with open(tmp, "w") as f:
            json.dump(command, f)
        os.replace(tmp, "studio_command.json")

    def emit_observatory_command(self, filepath=None, force=False):
        if not filepath:
            for i in range(self.step_tree.topLevelItemCount()):
                top_node = self.step_tree.topLevelItem(i)
                for j in range(top_node.childCount()):
                    child = top_node.child(j)
                    asset = child.data(1, Qt.ItemDataRole.UserRole)
                    if hasattr(asset, 'trigger') and asset.trigger.observatory_file:
                        if os.path.exists(asset.trigger.observatory_file):
                            filepath = asset.trigger.observatory_file
                            break
                    elif hasattr(asset, 'observatory_file') and asset.observatory_file:
                        if os.path.exists(asset.observatory_file):
                            filepath = asset.observatory_file
                            break
                    elif isinstance(asset, ObservatoryNodeData) and asset.filepath:
                        if os.path.exists(asset.filepath):
                            filepath = asset.filepath
                            break
                if filepath: break

        if filepath and (force or filepath != self.last_commanded_file) and os.path.exists(filepath):
            try:
                command = {"action": "load_and_start", "filepath": filepath, "timestamp": time.time()}
                self._write_command_atomic(command)
                self.last_commanded_file = filepath
                self.log_message(f"Commanded Observatory to load: {os.path.basename(filepath)}")
            except Exception as e:
                self.log_message(f"IPC Error: {e}")

    def emit_llm_trigger_command(self, item, force=False):
        if not item.observatory_file or not item.source_tool or item.source_tool == "None":
            return
        # Deliberately NOT calling emit_observatory_command() here: it would
        # write "studio_command.json" with a load_and_start command, and the
        # trigger_llm write immediately below would instantly overwrite it in
        # the same call stack - Observatory's 500ms polling timer can never
        # see the first write, so "make sure the right project is loaded"
        # never actually reached Observatory. Folding observatory_file into
        # the single trigger_llm command lets Observatory load it itself if
        # needed before firing the tool.
        try:
            command = {"action": "trigger_llm", "tool_name": item.source_tool, "prompt": item.prompt,
                       "observatory_file": item.observatory_file, "timestamp": time.time()}
            self._write_command_atomic(command)
            self.log_message(f"Commanded Observatory: run '{item.source_tool}' with LLM Call '{item.tool_name}' prompt.")
            # Arm the fresh-response tracking used by "Hold for Response":
            # anything already sitting in vision_tool_responses is stale
            # until a response_time newer than this timestamp comes back.
            item.response_pending = True
            item.trigger_sent_time = time.time()
            item.failsafe_triggered = False
            item.set_response("⏳ Waiting for Observatory response...")
        except Exception as e:
            self.log_message(f"IPC Error: {e}")

    def parse_observatory_file_silent(self, filepath):
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            return ["None"] + [t["name"] for t in data.get("tools", []) if "name" in t]
        except Exception:
            return ["None"]

    def _pause_engine_timers(self):
        self.master_clock.stop()
        self.ipc_timer.stop()

    def _resume_engine_timers(self):
        self.master_clock.start(100)
        self.ipc_timer.start(50)

    def prompt_open_file(self, title, filter_str, start_dir=""):
        # Construct QFileDialog explicitly and force the option directly on the
        # instance: the static getOpenFileName()/getSaveFileName() convenience
        # methods can silently ignore options=DontUseNativeDialog on Windows,
        # which left the native (COM-based) dialog in play despite passing it.
        #
        # Also stop our own timers for the dialog's lifetime: master_clock and
        # ipc_timer fire every 50-100ms and touch scene/Qt objects, while
        # QFileDialog spins up its own background threads for directory
        # scanning. Running both concurrently is a plausible trigger for the
        # STATUS_STACK_BUFFER_OVERRUN crash seen in Qt6Core.dll.
        self._pause_engine_timers()
        try:
            dialog = QFileDialog(self, title, start_dir, filter_str)
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                files = dialog.selectedFiles()
                if files:
                    return files[0]
            return None
        finally:
            self._resume_engine_timers()

    def prompt_save_file(self, title, filter_str):
        self._pause_engine_timers()
        try:
            dialog = QFileDialog(self, title, "", filter_str)
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                files = dialog.selectedFiles()
                if files:
                    return files[0]
            return None
        finally:
            self._resume_engine_timers()

    def save_project(self):
        path = self.prompt_save_file("Save Project", "Project Files (*.json)")
        if not path: return
        project_data = []

        for i in range(self.step_tree.topLevelItemCount()):
            top_node = self.step_tree.topLevelItem(i)
            step_block = {"type": "StepBlock", "children": []}
            for j in range(top_node.childCount()):
                child = top_node.child(j)
                asset = child.data(1, Qt.ItemDataRole.UserRole)
                if isinstance(asset, InteractiveTextItem):
                    step_block["children"].append(
                        {"type": "InteractiveTextItem", "pos": [asset.pos().x(), asset.pos().y()],
                         "text": asset.toPlainText(), "font_size": asset.current_font_size,
                         "text_color": asset.text_color.name(), "scale_factor": asset.scale_factor,
                         "scale_x": asset.scale_x, "scale_y": asset.scale_y,
                         "lock_aspect_ratio": asset.lock_aspect_ratio,
                         "is_blinking": asset.is_blinking, "curve_radius": asset.curve_radius,
                         "output_canvas": getattr(asset, "output_canvas", 0),
                         "animation_type": asset.animation_type, "animation_direction": asset.animation_direction,
                         "animation_duration": asset.animation_duration,
                         "rotation": asset.rotation(), "trigger": vars(asset.trigger)})
                elif isinstance(asset, InteractiveMediaItem):
                    step_block["children"].append(
                        {"type": "InteractiveMediaItem", "pos": [asset.pos().x(), asset.pos().y()],
                         "filepath": asset.filepath, "media_type": asset.media_type, "rotation": asset.rotation(),
                         "scale_factor": asset.scale_factor, "time_hold": asset.time_hold,
                         "output_canvas": getattr(asset, "output_canvas", 0),
                         "scale_x": asset.scale_x, "scale_y": asset.scale_y,
                         "lock_aspect_ratio": asset.lock_aspect_ratio,
                         "is_blinking": asset.is_blinking,
                         "animation_type": asset.animation_type, "animation_direction": asset.animation_direction,
                         "animation_duration": asset.animation_duration,
                         "trigger": vars(asset.trigger)})
                elif isinstance(asset, InteractiveShapeItem):
                    step_block["children"].append(
                        {"type": "InteractiveShapeItem", "pos": [asset.pos().x(), asset.pos().y()],
                         "shape_type": asset.shape_type, "svg_path": asset.svg_path,
                         "output_canvas": getattr(asset, "output_canvas", 0),
                         "rotation": asset.rotation(), "scale_factor": asset.scale_factor,
                         "scale_x": asset.scale_x, "scale_y": asset.scale_y,
                         "lock_aspect_ratio": asset.lock_aspect_ratio,
                         "outline_width": asset.outline_width, "outline_color": asset.outline_color.name(),
                         "fill_enabled": asset.fill_enabled, "fill_color": asset.fill_color.name(),
                         "is_blinking": asset.is_blinking,
                         "animation_type": asset.animation_type, "animation_direction": asset.animation_direction,
                         "animation_duration": asset.animation_duration,
                         "trigger": vars(asset.trigger)})
                elif isinstance(asset, InteractiveHTMLItem):
                    step_block["children"].append(
                        {"type": "InteractiveHTMLItem", "pos": [asset.pos().x(), asset.pos().y()],
                         "url": asset.url, "width": asset.base_width, "height": asset.base_height,
                         "output_canvas": getattr(asset, "output_canvas", 0),
                         "rotation": asset.rotation(), "trigger": vars(asset.trigger)})
                elif isinstance(asset, Interactive3DModelItem):
                    step_block["children"].append(
                        {"type": "Interactive3DModelItem", "pos": [asset.pos().x(), asset.pos().y()],
                         "filepath": asset.filepath,
                         "width": asset.rect().width(), "height": asset.rect().height(),
                         "rot_x": asset.rot_x, "rot_y": asset.rot_y, "rot_z": asset.rot_z,
                         "model_scale": asset.model_scale,
                         "model_tx": asset.model_tx, "model_ty": asset.model_ty,
                         "model_color": asset.model_color.name(),
                         "home_pos": [asset.home_position().x(), asset.home_position().y()],
                         "scale_x": asset.scale_x, "scale_y": asset.scale_y,
                         "scale_factor": asset.scale_factor,
                         "lock_aspect_ratio": asset.lock_aspect_ratio,
                         "is_blinking": asset.is_blinking,
                         "animation_type": asset.animation_type,
                         "animation_direction": asset.animation_direction,
                         "animation_duration": asset.animation_duration,
                         "output_canvas": getattr(asset, "output_canvas", 0),
                         "rotation": asset.rotation(), "trigger": vars(asset.trigger)})
                elif isinstance(asset, InteractiveCaptureItem):
                    step_block["children"].append(
                        {"type": "InteractiveCaptureItem", "pos": [asset.pos().x(), asset.pos().y()],
                         "item_name": asset.item_name, "observatory_file": asset.observatory_file,
                         "source_tool": asset.source_tool,
                         "source_variant": getattr(asset, "source_variant", "Captured"),
                         "image_path": asset.image_path,
                         "scale_x": asset.scale_x, "scale_y": asset.scale_y,
                         "scale_factor": asset.scale_factor,
                         "lock_aspect_ratio": asset.lock_aspect_ratio,
                         "is_blinking": asset.is_blinking,
                         "animation_type": asset.animation_type,
                         "animation_direction": asset.animation_direction,
                         "animation_duration": asset.animation_duration,
                         "output_canvas": getattr(asset, "output_canvas", 0),
                         "rotation": asset.rotation(), "trigger": vars(asset.trigger)})
                elif isinstance(asset, InteractiveLLMTextItem):
                    step_block["children"].append(
                        {"type": "InteractiveLLMTextItem", "pos": [asset.pos().x(), asset.pos().y()],
                         "tool_name": asset.tool_name, "observatory_file": asset.observatory_file,
                         "output_canvas": getattr(asset, "output_canvas", 0),
                         "source_tool": asset.source_tool, "prompt": asset.prompt,
                         "font_size": asset.current_font_size, "text_color": asset.text_color.name(),
                         "width": asset.rect().width(), "height": asset.rect().height(),
                         "hold_for_response": asset.hold_for_response,
                         "rotation": asset.rotation(), "trigger": vars(asset.trigger)})
                elif isinstance(asset, FlowControlData):
                    step_block["children"].append(
                        {"type": "FlowControlData", "action": asset.action, "target_step": asset.target_step})
                elif isinstance(asset, TimerData):
                    step_block["children"].append({"type": "TimerData", "duration": asset.duration})
                elif isinstance(asset, ObservatoryNodeData):
                    step_block["children"].append({"type": "ObservatoryNodeData", "filepath": asset.filepath})
                elif isinstance(asset, ClearCanvasData):
                    step_block["children"].append(
                        {"type": "ClearCanvasData",
                         "output_canvas": getattr(asset, 'output_canvas', -1)})
                elif isinstance(asset, VisionWaitData):
                    step_block["children"].append({"type": "VisionWaitData", "observatory_file": asset.observatory_file,
                                                   "vision_target": asset.vision_target, "condition": asset.condition})
                elif isinstance(asset, UserWaitData):
                    step_block["children"].append(
                        {"type": "UserWaitData", "forward_action": asset.forward_action,
                         "forward_target_step": asset.forward_target_step,
                         "backward_action": asset.backward_action,
                         "backward_target_step": asset.backward_target_step})
            project_data.append(step_block)

        with open(path, 'w') as f:
            json.dump(project_data, f, indent=4)
        self.log_message(f"Project saved to {os.path.basename(path)}")

    def add_loaded_asset(self, new_item, child_data):
        """Place a just-loaded asset on the output canvas it was saved to,
        falling back to canvas 0 if that canvas no longer exists."""
        idx = int(child_data.get('output_canvas', 0))
        idx = max(0, min(len(self.canvases) - 1, idx))
        new_item.output_canvas = idx
        self.canvases[idx].scene.addItem(new_item)

    def ensure_canvas_capacity(self, project_data):
        """Grow the canvas count so every asset's saved output_canvas index
        actually has a canvas to land on."""
        needed = 1
        for block in project_data:
            for child in block.get("children", []):
                needed = max(needed, int(child.get("output_canvas", 0)) + 1)
        if needed > len(self.canvases):
            self.set_canvas_count(needed)
            self.cb_canvas_count.blockSignals(True)
            self.cb_canvas_count.setCurrentText(str(len(self.canvases)))
            self.cb_canvas_count.blockSignals(False)

    def load_project(self):
        path = self.prompt_open_file("Load Project", "Project Files (*.json)")
        if not path: return
        self.active_item = None
        for c in self.canvases:
            c.clear_canvas()
        self.step_tree.clear()

        try:
            with open(path, 'r') as f:
                project_data = json.load(f)

            self.ensure_canvas_capacity(project_data)

            if len(project_data) > 0 and project_data[0].get("type") != "StepBlock":
                self.log_message(
                    "<span style='color:red;'>Outdated project format detected. Please reconstruct sequence.</span>")
                self.new_file()
                return

            for idx, step_block in enumerate(project_data):
                top_item = QTreeWidgetItem(self.step_tree, ["", f"Step Block [{idx + 1}]", "", ""])
                top_item.setBackground(1, QColor("#1e508c"))
                top_item.setForeground(1, QColor("white"))
                font = QFont()
                font.setBold(True)
                top_item.setFont(1, font)

                for child_data in step_block.get("children", []):
                    ctype = child_data.get("type")
                    if ctype == "InteractiveTextItem":
                        new_item = InteractiveTextItem(child_data.get('text', "Text"))
                        new_item.current_font_size = child_data.get('font_size', 48)
                        new_item.setFont(QFont("Arial", new_item.current_font_size))
                        new_item.set_text_color(QColor(child_data.get('text_color', "#ffffff")))
                        self.load_scale_state(new_item, child_data)
                        new_item.is_blinking = child_data.get('is_blinking', False)
                        new_item.set_curve_radius(child_data.get('curve_radius', 0))
                        new_item.animation_type = child_data.get('animation_type', "None")
                        new_item.animation_direction = child_data.get('animation_direction', "Left")
                        new_item.animation_duration = child_data.get('animation_duration', 1.0)
                        new_item.trigger.__dict__.update(child_data.get('trigger', {}))
                        new_item.setPos(*child_data.get('pos', [200.0, 200.0]))
                        new_item.setRotation(child_data.get('rotation', 0))
                        self.add_loaded_asset(new_item, child_data)
                        ti = QTreeWidgetItem(top_item, ["", "✏️ Text Element", "Active in Step", ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, new_item)

                    elif ctype == "InteractiveMediaItem":
                        new_item = InteractiveMediaItem(child_data.get('filepath', ""),
                                                        child_data.get('media_type', "Image"))
                        new_item.trigger.__dict__.update(child_data.get('trigger', {}))
                        new_item.setPos(*child_data.get('pos', [200.0, 200.0]))
                        new_item.setRotation(child_data.get('rotation', 0))
                        self.load_scale_state(new_item, child_data)
                        new_item.time_hold = child_data.get('time_hold', False)
                        new_item.is_blinking = child_data.get('is_blinking', False)
                        new_item.animation_type = child_data.get('animation_type', "None")
                        new_item.animation_direction = child_data.get('animation_direction', "Left")
                        new_item.animation_duration = child_data.get('animation_duration', 1.0)
                        self.add_loaded_asset(new_item, child_data)
                        ti = QTreeWidgetItem(top_item, ["", f"Media [{new_item.media_type}]", "Active in Step", ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, new_item)

                    elif ctype == "InteractiveShapeItem":
                        new_item = InteractiveShapeItem(child_data.get('shape_type', "Square"),
                                                        child_data.get('svg_path', ""))
                        new_item.trigger.__dict__.update(child_data.get('trigger', {}))
                        new_item.setPos(*child_data.get('pos', [200.0, 200.0]))
                        new_item.setRotation(child_data.get('rotation', 0))
                        self.load_scale_state(new_item, child_data)
                        new_item.set_outline_width(child_data.get('outline_width', 4))
                        new_item.set_outline_color(QColor(child_data.get('outline_color', "#ffffff")))
                        new_item.set_fill_enabled(child_data.get('fill_enabled', False))
                        new_item.set_fill_color(QColor(child_data.get('fill_color', "#3498db")))
                        new_item.is_blinking = child_data.get('is_blinking', False)
                        new_item.animation_type = child_data.get('animation_type', "None")
                        new_item.animation_direction = child_data.get('animation_direction', "Left")
                        new_item.animation_duration = child_data.get('animation_duration', 1.0)
                        self.add_loaded_asset(new_item, child_data)
                        ti = QTreeWidgetItem(top_item, ["", f"◆ Shape [{new_item.shape_type}]", "Active in Step", ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, new_item)

                    elif ctype == "InteractiveHTMLItem":
                        new_item = InteractiveHTMLItem(child_data.get('url', ""))
                        new_item.trigger.__dict__.update(child_data.get('trigger', {}))
                        new_item.setPos(*child_data.get('pos', [200.0, 200.0]))
                        new_item.setRotation(child_data.get('rotation', 0))
                        new_item.resize_by_drag(child_data.get('width', 480), child_data.get('height', 360))
                        self.add_loaded_asset(new_item, child_data)
                        ti = QTreeWidgetItem(top_item, ["", "🌐 HTML Window", "Active in Step", ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, new_item)

                    elif ctype == "Interactive3DModelItem":
                        new_item = Interactive3DModelItem(
                            "",   # loaded below so a missing/slow file can't stall the project load
                            child_data.get('width', 480), child_data.get('height', 360))
                        new_item.trigger.__dict__.update(child_data.get('trigger', {}))
                        new_item.setPos(*child_data.get('pos', [200.0, 200.0]))
                        new_item.setRotation(child_data.get('rotation', 0))
                        new_item.rot_x = child_data.get('rot_x', 20.0)
                        new_item.rot_y = child_data.get('rot_y', -30.0)
                        new_item.rot_z = child_data.get('rot_z', 0.0)
                        new_item.model_scale = child_data.get('model_scale', 1.0)
                        new_item.model_tx = child_data.get('model_tx', 0.0)
                        new_item.model_ty = child_data.get('model_ty', 0.0)
                        new_item.model_color = QColor(child_data.get('model_color', "#4aa3df"))
                        hp = child_data.get('home_pos')
                        new_item.set_home_position(QPointF(hp[0], hp[1]) if hp else None)
                        self.load_scale_state(new_item, child_data)
                        new_item.is_blinking = child_data.get('is_blinking', False)
                        new_item.animation_type = child_data.get('animation_type', "None")
                        new_item.animation_direction = child_data.get('animation_direction', "Left")
                        new_item.animation_duration = child_data.get('animation_duration', 1.0)
                        saved_path = child_data.get('filepath', "")
                        if saved_path and os.path.exists(saved_path):
                            # Async: a face-heavy STEP can take a minute-plus,
                            # and the project must finish loading regardless.
                            new_item.load_model_async(saved_path)
                        elif saved_path:
                            new_item.load_error = f"File not found: {os.path.basename(saved_path)}"
                            new_item.filepath = saved_path
                        self.add_loaded_asset(new_item, child_data)
                        ti = QTreeWidgetItem(top_item, ["", "🧊 3D Model", "Active in Step", ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, new_item)

                    elif ctype == "InteractiveCaptureItem":
                        new_item = InteractiveCaptureItem(
                            child_data.get("item_name", "Capture"),
                            child_data.get("observatory_file", ""),
                            child_data.get("source_tool", "None"))
                        new_item.trigger.__dict__.update(child_data.get("trigger", {}))
                        new_item.source_variant = child_data.get("source_variant", "Captured")
                        new_item.setPos(*child_data.get("pos", [200.0, 200.0]))
                        new_item.setRotation(child_data.get("rotation", 0))
                        new_item.load_capture(child_data.get("image_path", ""))
                        self.load_scale_state(new_item, child_data)
                        new_item.is_blinking = child_data.get("is_blinking", False)
                        new_item.animation_type = child_data.get("animation_type", "None")
                        new_item.animation_direction = child_data.get("animation_direction", "Left")
                        new_item.animation_duration = child_data.get("animation_duration", 1.0)
                        self.add_loaded_asset(new_item, child_data)
                        ti = QTreeWidgetItem(top_item, ["", "📷 Capture Image", "Active in Step", ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, new_item)

                    elif ctype == "InteractiveLLMTextItem":
                        new_item = InteractiveLLMTextItem(child_data.get('tool_name', "LLM Call"),
                                                          child_data.get('observatory_file', ""),
                                                          child_data.get('source_tool', "None"),
                                                          child_data.get('prompt', "Describe what is inside this image crop."))
                        new_item.set_font_size(child_data.get('font_size', 16))
                        new_item.set_text_color(QColor(child_data.get('text_color', "#00ff99")))
                        new_item.update_size(child_data.get('width', 420), child_data.get('height', 220))
                        new_item.hold_for_response = child_data.get('hold_for_response', False)
                        new_item.trigger.__dict__.update(child_data.get('trigger', {}))
                        new_item.setPos(*child_data.get('pos', [200.0, 200.0]))
                        new_item.setRotation(child_data.get('rotation', 0))
                        self.add_loaded_asset(new_item, child_data)
                        ti = QTreeWidgetItem(top_item, ["", "🧠 LLM Call", "Active in Step", ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, new_item)

                    elif ctype == "FlowControlData":
                        data = FlowControlData(child_data.get("action"), child_data.get("target_step"))
                        ti = QTreeWidgetItem(top_item, ["", "🛑 Flow Control", f"Action: {data.action}", ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, data)
                        ti.setForeground(1, QColor("#e74c3c"))

                    elif ctype == "TimerData":
                        data = TimerData(child_data.get("duration", 5.0))
                        ti = QTreeWidgetItem(top_item, ["", "⏳ Timer", f"Wait {data.duration} sec", ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, data)
                        ti.setForeground(1, QColor("#d35400"))

                    elif ctype == "ObservatoryNodeData":
                        data = ObservatoryNodeData(child_data.get("filepath", ""))
                        fname = os.path.basename(data.filepath) if data.filepath else "No File"
                        ti = QTreeWidgetItem(top_item, ["", "👁️ Load Observatory Project", fname, ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, data)
                        ti.setForeground(1, QColor("#2980b9"))

                    elif ctype == "ClearCanvasData":
                        data = ClearCanvasData()
                        data.output_canvas = child_data.get("output_canvas", -1)
                        target = ("All Canvases" if data.output_canvas < 0
                                  else f"Canvas {data.output_canvas + 1}")
                        ti = QTreeWidgetItem(top_item, ["", "🧹 Clear Canvas", target, ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, data)

                    elif ctype == "VisionWaitData":
                        data = VisionWaitData()
                        data.observatory_file = child_data.get("observatory_file", "")
                        data.vision_target = child_data.get("vision_target", "None")
                        data.condition = child_data.get("condition", "Is True")
                        ti = QTreeWidgetItem(top_item, ["", "⏳ Wait for Vision", f"Target: {data.vision_target}", ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, data)
                        ti.setForeground(1, QColor("#8e44ad"))

                    elif ctype == "UserWaitData":
                        data = UserWaitData()
                        data.forward_action = child_data.get("forward_action", "Next Step")
                        data.forward_target_step = child_data.get("forward_target_step", 1)
                        data.backward_action = child_data.get("backward_action", "Previous Step")
                        data.backward_target_step = child_data.get("backward_target_step", 1)
                        ti = QTreeWidgetItem(top_item, ["", "👆 User Wait",
                                                        f"Fwd: {data.forward_action} | Back: {data.backward_action}", ""])
                        ti.setData(1, Qt.ItemDataRole.UserRole, data)
                        ti.setForeground(1, QColor("#16a085"))

                top_item.setExpanded(True)
            self.renumber_sequence()
            self.log_message("Project loaded.")

        except Exception as e:
            self.log_message(f"<span style='color:red;'>Failed to load project: {e}</span>")

    def setup_ui(self):
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")
        file_menu.addAction("New Project", self.new_project)
        file_menu.addSeparator()
        file_menu.addAction("Save Project", self.save_project)
        file_menu.addAction("Load Project", self.load_project)

        main_layout = QVBoxLayout()
        central = QWidget()
        central.setLayout(main_layout)
        self.setCentralWidget(central)

        toolbar = QHBoxLayout()
        self.btn_design_mode = QPushButton("✏️ DESIGN MODE")
        self.btn_design_mode.clicked.connect(self.enter_design_mode)
        self.btn_run_mode = QPushButton("▶️ RUN MODE")
        self.btn_run_mode.clicked.connect(self.enter_run_mode)
        self.btn_restart = QPushButton("⏮️ RESTART")
        self.btn_restart.clicked.connect(self.restart_sequence)
        self.btn_play = QPushButton("▶️ PLAY")
        self.btn_play.clicked.connect(self.play_sequence)
        self.btn_stop = QPushButton("⏸️ STOP")
        self.btn_stop.clicked.connect(self.stop_sequence)

        for btn in [self.btn_restart, self.btn_play, self.btn_stop]:
            btn.setVisible(False)

        btn_launch_obs = QPushButton("👁️ Launch Observatory")
        btn_launch_obs.setStyleSheet("background-color: #3498db; font-weight: bold; color: white;")
        btn_launch_obs.clicked.connect(self.launch_observatory)
        self.chk_auto_launch = QCheckBox("Auto-launch at startup")
        self.chk_auto_launch.setToolTip(
            "Start Observatory automatically when Studio opens.")
        self.chk_auto_launch.setChecked(
            self.settings.value("auto_launch_observatory", False, type=bool))
        self.chk_auto_launch.toggled.connect(
            lambda v: self.settings.setValue("auto_launch_observatory", bool(v)))

        self.lbl_canvas_count = QLabel("  Canvases:")
        self.cb_canvas_count = QComboBox()
        self.cb_canvas_count.addItems([str(n) for n in range(1, 6)])
        self.cb_canvas_count.setCurrentText("1")
        self.cb_canvas_count.currentTextChanged.connect(
            lambda v: self.safe_slot(self.set_canvas_count, int(v))())

        toolbar.addWidget(self.btn_design_mode)
        toolbar.addWidget(self.btn_run_mode)
        toolbar.addWidget(self.lbl_canvas_count)
        toolbar.addWidget(self.cb_canvas_count)
        toolbar.addWidget(self.btn_restart)
        toolbar.addWidget(self.btn_play)
        toolbar.addWidget(self.btn_stop)
        toolbar.addStretch()
        toolbar.addWidget(btn_launch_obs)
        toolbar.addWidget(self.chk_auto_launch)
        main_layout.addLayout(toolbar)

        v_splitter = QSplitter(Qt.Orientation.Vertical)
        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        l_lay = QVBoxLayout(left)

        btn_step = QPushButton("🟦 Add Step Block")
        btn_step.clicked.connect(self.add_new_step_node)
        btn_txt = QPushButton("+ Insert Text Element")
        btn_txt.clicked.connect(lambda: self.insert_asset(InteractiveTextItem("Text"), "✏️ Text Element"))
        btn_img = QPushButton("+ Insert Image Asset")
        btn_img.clicked.connect(lambda: self.browse_media("Image"))
        btn_vid = QPushButton("+ Insert Video Element")
        btn_vid.clicked.connect(lambda: self.browse_media("Video"))
        btn_shape = QPushButton("◆ Insert Shape")
        btn_shape.clicked.connect(self.insert_shape_dialog)
        btn_shape.setStyleSheet("background-color: #27ae60; font-weight: bold;")
        btn_html = QPushButton("🌐 Insert HTML Window")
        btn_html.clicked.connect(self.insert_html_dialog)
        btn_html.setStyleSheet("background-color: #34495e; font-weight: bold;")
        btn_model3d = QPushButton("🧊 Insert 3D Model")
        btn_model3d.clicked.connect(self.insert_3d_model)
        btn_model3d.setStyleSheet("background-color: #4aa3df; font-weight: bold; color: black;")
        btn_capture = QPushButton("📷 Insert Capture Image")
        btn_capture.clicked.connect(self.insert_capture_image)
        btn_capture.setStyleSheet("background-color: #f39c12; font-weight: bold; color: black;")
        btn_llm = QPushButton("🧠 Insert LLM Call")
        btn_llm.clicked.connect(self.insert_llm_call)
        btn_llm.setStyleSheet("background-color: #16a085; font-weight: bold;")
        btn_clear = QPushButton("🧹 Insert Clear Canvas")
        btn_clear.clicked.connect(self.insert_clear_canvas)
        btn_clear.setStyleSheet("background-color: #7f8c8d; font-weight: bold;")
        btn_timer = QPushButton("⏳ Insert Timer")
        btn_timer.clicked.connect(self.insert_timer)
        btn_timer.setStyleSheet("background-color: #d35400; font-weight: bold;")
        btn_obs = QPushButton("👁️ Insert Observatory Node")
        btn_obs.clicked.connect(self.insert_observatory_node)
        btn_obs.setStyleSheet("background-color: #2980b9; font-weight: bold;")
        btn_wait = QPushButton("⏳ Insert Vision Wait")
        btn_wait.clicked.connect(self.insert_vision_wait)
        btn_wait.setStyleSheet("background-color: #8e44ad; font-weight: bold;")
        btn_flow = QPushButton("🛑 Insert Flow Control")
        btn_flow.clicked.connect(self.insert_flow_control)
        btn_flow.setStyleSheet("background-color: #c0392b; font-weight: bold;")
        btn_user_wait = QPushButton("👆 Insert User Wait")
        btn_user_wait.clicked.connect(self.insert_user_wait)
        btn_user_wait.setStyleSheet("background-color: #16a085; font-weight: bold;")

        # Grouped into one container so it can be hidden as a unit in Run
        # Mode, where inserting/editing sequence items doesn't make sense.
        self.insert_tools_container = QWidget()
        insert_lay = QVBoxLayout(self.insert_tools_container)
        insert_lay.setContentsMargins(0, 0, 0, 0)
        for b in [btn_step, btn_txt, btn_img, btn_vid, btn_shape, btn_html, btn_model3d, btn_capture, btn_llm, btn_clear, btn_timer, btn_obs, btn_wait, btn_flow, btn_user_wait]:
            insert_lay.addWidget(b)
        l_lay.addWidget(self.insert_tools_container)

        # --- AI ASSISTANT BLOCK RESTORED ---
        self.ai_group = QGroupBox("🧠 AI Assistant")
        ai_lay = QVBoxLayout(self.ai_group)
        self.llm_settings = LLMProviderSettingsWidget(self.settings)
        ai_lay.addWidget(self.llm_settings)

        self.ai_prompt = QTextEdit()
        self.ai_prompt.setPlaceholderText("Describe the sequence you want...")
        self.ai_prompt.setMaximumHeight(60)
        ai_lay.addWidget(self.ai_prompt)

        self.btn_ask_ai = QPushButton("✨ Generate Sequence")
        self.btn_ask_ai.setStyleSheet("background-color: #8e44ad; font-weight: bold; color: white;")
        self.btn_ask_ai.clicked.connect(self.trigger_ai)
        ai_lay.addWidget(self.btn_ask_ai)
        # Built here alongside the other left-panel widgets, but actually
        # PLACED into the right-hand panel's bottom (see self.r_lay below).
        # -----------------------------------

        # Read-only mirror of the sequence, shown in this same spot only
        # while Run Mode is active (the insert tools/AI assistant above are
        # hidden then) - the currently-executing Step Block gets highlighted
        # every tick so you can see exactly where playback is without
        # switching back to Design Mode.
        self.run_sequence_view = QTreeWidget()
        self.run_sequence_view.setHeaderLabels(["#", "Sequence Pipeline", "Action / Trigger", "Properties"])
        self.run_sequence_view.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.run_sequence_view.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.run_sequence_view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.run_sequence_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.run_sequence_view.setVisible(False)
        l_lay.addWidget(self.run_sequence_view)

        self.btn_del_line = QPushButton("🗑️ Delete Selected Line")
        self.btn_del_line.clicked.connect(self.delete_selected_tree_node)
        l_lay.addStretch()
        l_lay.addWidget(self.btn_del_line)
        h_splitter.addWidget(left)

        center = QWidget()
        c_lay = QVBoxLayout(center)
        self.step_tree = SequenceTreeWidget(self)
        self.run_preview_view = RunPreviewView(self.studio_scene)

        self.run_view_container = QWidget()
        run_view_lay = QVBoxLayout(self.run_view_container)
        run_view_lay.setContentsMargins(0, 0, 0, 0)
        run_view_lay.setSpacing(4)

        user_wait_row = QHBoxLayout()
        self.btn_user_wait_back = QPushButton("◀ BACK")
        self.btn_user_wait_back.setMinimumHeight(60)
        self.btn_user_wait_back.setStyleSheet(
            "font-size: 18px; font-weight: bold; background-color: #34495e; color: white;")
        self.btn_user_wait_back.clicked.connect(lambda: self._handle_user_wait_button("backward"))
        self.btn_user_wait_back.setEnabled(False)

        self.btn_user_wait_forward = QPushButton("FORWARD ▶")
        self.btn_user_wait_forward.setMinimumHeight(60)
        self.btn_user_wait_forward.setStyleSheet(
            "font-size: 18px; font-weight: bold; background-color: #2980b9; color: white;")
        self.btn_user_wait_forward.clicked.connect(lambda: self._handle_user_wait_button("forward"))
        self.btn_user_wait_forward.setEnabled(False)

        user_wait_row.addWidget(self.btn_user_wait_back)
        user_wait_row.addWidget(self.btn_user_wait_forward)
        run_view_lay.addLayout(user_wait_row)

        # With more than one output canvas the preview can only mirror one
        # scene at a time, so let the operator pick which. Hidden entirely
        # in the common single-canvas case rather than showing a pointless
        # one-entry dropdown.
        preview_row = QHBoxLayout()
        self.lbl_preview_canvas = QLabel("Previewing:")
        self.cb_preview_canvas = QComboBox()
        self.cb_preview_canvas.currentIndexChanged.connect(self.set_preview_canvas)
        preview_row.addWidget(self.lbl_preview_canvas)
        preview_row.addWidget(self.cb_preview_canvas)
        preview_row.addStretch()
        run_view_lay.addLayout(preview_row)
        run_view_lay.addWidget(self.run_preview_view)
        self.refresh_preview_canvas_selector()

        self.center_stack = QStackedWidget()
        self.center_stack.addWidget(self.step_tree)
        self.center_stack.addWidget(self.run_view_container)
        c_lay.addWidget(self.center_stack)
        h_splitter.addWidget(center)

        right = QWidget()
        self.r_lay = QVBoxLayout(right)
        self.prop_group = QGroupBox("Properties")
        self.prop_form = QFormLayout(self.prop_group)
        self.r_lay.addWidget(self.prop_group)
        self.node_prop_group = QGroupBox("Node Settings")
        self.node_prop_form = QFormLayout(self.node_prop_group)
        self.r_lay.addWidget(self.node_prop_group)

        self.prop_group.setVisible(False)
        self.node_prop_group.setVisible(False)

        self.r_lay.addStretch()
        self.r_lay.addWidget(self.ai_group)
        h_splitter.addWidget(right)
        h_splitter.setSizes([220, 800, 350])

        self.console = QTextBrowser()
        self.console.setReadOnly(True)
        v_splitter.addWidget(h_splitter)
        v_splitter.addWidget(self.console)
        v_splitter.setSizes([650, 150])
        main_layout.addWidget(v_splitter)

        self.studio_override_view = StudioCanvasView(self.studio_scene, self)
        self.canvas.setCentralWidget(self.studio_override_view)
        self.new_file()

    def trigger_ai(self):
        config = self.llm_settings.get_config()
        user_prompt = self.ai_prompt.toPlainText().strip()
        config_ready = (config["provider"] == "Gemini" and config.get("api_key")) or \
                        (config["provider"] == "OpenAI-Compatible" and config.get("base_url") and config.get("model"))
        if not config_ready or not user_prompt:
            self.log_message("<span style='color:orange;'>AI Error: Missing LLM provider settings or Prompt.</span>")
            return

        self.log_message("Consulting AI Sequence Engineer...")
        self.btn_ask_ai.setEnabled(False)

        # --- EXPERT SYSTEM PROMPT WITH OBSERVATORY KNOWLEDGE ---
        system_context = """You are an expert sequence programmer for a spatial computing and augmented reality manufacturing platform. 
Your job is to generate sequence pipelines based on the user's request. 

### SYSTEM KNOWLEDGE: THE OBSERVATORY ENGINE
You must design sequences that utilize the platform's vision engine ("Observatory"). 
- Observatory tools are created by selecting a tool and double-clicking the camera feed to generate a Region of Interest (ROI). The tool receives an auto-assigned name (e.g., "tool01_Moti" for motion, "tool02_Pattern" for pattern match).
- Pre-processing filters (Threshold, Edge Detect) are used to convert feeds to B&W or outlines to improve tool accuracy.
- **Motion Detection:** Excellent for proximity/presence. Requires training via "Capture Reference State". Key parameters are Minimum Motion Area and Pixel Difference Threshold (usually ~20%).
- **Pattern Match:** Uses two ROIs. The outer box is the Search Area; the inner box is the target pattern to match. Excellent for tracking or specific part presence.
- **Integration:** The user saves these tools in an Observatory file (e.g., "project.json"). The Authoring sequence MUST load this file using an ObservatoryNodeData, and then use VisionWaitData or Interactive item triggers to listen for the specific tool names to become "Is True" or "Is False".

### OUTPUT FORMAT
You MUST respond ONLY with a valid JSON array of StepBlocks. Do not use markdown formatting (like ```json), just output the raw JSON text. Use descriptive text in InteractiveTextItems to guide the user.

Here is the exact template structure you must follow:
[
    {
        "type": "StepBlock",
        "children": [
            { "type": "ClearCanvasData" },
            { "type": "ObservatoryNodeData", "filepath": "C:/Users/mikes/PycharmProjects/lightproject/my_obs_file.json" },
            { "type": "VisionWaitData", "observatory_file": "C:/Users/mikes/PycharmProjects/lightproject/my_obs_file.json", "vision_target": "tool01_Moti", "condition": "Is True" }
        ]
    },
    {
        "type": "StepBlock",
        "children": [
            { "type": "InteractiveTextItem", "pos": [200.0, 200.0], "text": "Motion Detected! Proceeding...", "font_size": 48, "rotation": 0.0, "trigger": { "wait_type": "Line", "observatory_file": "", "vision_target": "None", "condition": "Is True" } }
        ]
    }
]

Create an intelligent, multi-step JSON sequence that accomplishes the following user request, incorporating Observatory logic if applicable: """

        full_prompt = system_context + "\n\n" + user_prompt

        try:
            self.llm_worker = LLMWorker(config, full_prompt)
            self.llm_worker.finished.connect(self.handle_ai_response)
            self.llm_worker.start()
        except Exception as e:
            self.log_message(f"<span style='color:red;'>AI Worker Error: {e}</span>")
            self.btn_ask_ai.setEnabled(True)

    def handle_ai_response(self, response_text):
        self.btn_ask_ai.setEnabled(True)

        # Clean up the string in case the LLM ignored the "no markdown" rule
        clean_json_str = response_text.strip()
        if clean_json_str.startswith("```json"):
            clean_json_str = clean_json_str.split("```json")[1]
        if clean_json_str.startswith("```"):
            clean_json_str = clean_json_str.split("```")[1]
        if clean_json_str.endswith("```"):
            clean_json_str = clean_json_str.rsplit("```", 1)[0]

        clean_json_str = clean_json_str.strip()

        try:
            new_sequence = json.loads(clean_json_str)

            # Auto-build the sequence tree from the AI's JSON
            for step_block in new_sequence:
                if step_block.get("type") == "StepBlock":
                    self.add_new_step_node()
                    parent = self.step_tree.topLevelItem(self.step_tree.topLevelItemCount() - 1)

                    for child_data in step_block.get("children", []):
                        ctype = child_data.get("type")

                        if ctype == "ClearCanvasData":
                            ti = QTreeWidgetItem(parent, ["", "🧹 Clear Canvas", "Executes Immediately", ""])
                            ti.setData(1, Qt.ItemDataRole.UserRole, ClearCanvasData())

                        elif ctype == "VisionWaitData":
                            data = VisionWaitData()
                            data.observatory_file = child_data.get("observatory_file", "")
                            data.vision_target = child_data.get("vision_target", "None")
                            data.condition = child_data.get("condition", "Is True")
                            ti = QTreeWidgetItem(parent, ["", "⏳ Wait for Vision", f"Target: {data.vision_target}", ""])
                            ti.setData(1, Qt.ItemDataRole.UserRole, data)
                            ti.setForeground(1, QColor("#8e44ad"))

                        elif ctype == "InteractiveTextItem":
                            new_item = InteractiveTextItem(child_data.get('text', "Text"))
                            new_item.current_font_size = child_data.get('font_size', 48)
                            new_item.setFont(QFont("Arial", new_item.current_font_size))
                            new_item.trigger.__dict__.update(child_data.get('trigger', {}))
                            new_item.setPos(*child_data.get('pos', [200.0, 200.0]))
                            new_item.setRotation(child_data.get('rotation', 0))
                            self.add_loaded_asset(new_item, child_data)
                            ti = QTreeWidgetItem(parent, ["", "✏️ Text Element", "Active in Step", ""])
                            ti.setData(1, Qt.ItemDataRole.UserRole, new_item)

                        elif ctype == "TimerData":
                            data = TimerData(child_data.get("duration", 5.0))
                            ti = QTreeWidgetItem(parent, ["", "⏳ Timer", f"Wait {data.duration} sec", ""])
                            ti.setData(1, Qt.ItemDataRole.UserRole, data)
                            ti.setForeground(1, QColor("#d35400"))

                        elif ctype == "FlowControlData":
                            data = FlowControlData(child_data.get("action"), child_data.get("target_step"))
                            ti = QTreeWidgetItem(parent, ["", "🛑 Flow Control", f"Action: {data.action}", ""])
                            ti.setData(1, Qt.ItemDataRole.UserRole, data)
                            ti.setForeground(1, QColor("#e74c3c"))

                        elif ctype == "ObservatoryNodeData":
                            data = ObservatoryNodeData(child_data.get("filepath", ""))
                            fname = os.path.basename(data.filepath) if data.filepath else "No File"
                            ti = QTreeWidgetItem(parent, ["", "👁️ Load Observatory Project", fname, ""])
                            ti.setData(1, Qt.ItemDataRole.UserRole, data)
                            ti.setForeground(1, QColor("#2980b9"))

            self.renumber_sequence()
            self.log_message("AI successfully generated and inserted sequence blocks.")

        except json.JSONDecodeError:
            # Fallback: If it's not valid JSON, just paste it as a text item like the old version
            self.log_message(
                "<span style='color:orange;'>AI response was not JSON. Falling back to text element.</span>")
            self.insert_asset(InteractiveTextItem(response_text), "✏️ AI Text")
        except Exception as e:
            self.log_message(f"<span style='color:red;'>Error parsing AI response: {e}</span>")

    def launch_observatory(self):
        # Use an absolute path + explicit cwd for both the script and the
        # working directory: a bare relative "main.py" only resolves if the
        # *current* process's working directory happens to already be this
        # project folder. If it isn't (e.g. a different IDE run config),
        # Popen() itself still succeeds (python.exe is a valid absolute
        # path), but the child process silently fails to find main.py and
        # exits - invisible to this method's own try/except, since that
        # failure happens inside the child, not in the Popen() call. Worse,
        # even if main.py were found some other way, a mismatched cwd means
        # Observatory would read/write vision_state.json and
        # studio_command.json in a different folder than Studio does,
        # making the file-based IPC silently invisible to it.
        script_dir = os.path.dirname(os.path.abspath(__file__))
        main_script = os.path.join(script_dir, "main.py")
        if not os.path.exists(main_script):
            self.log_message(f"<span style='color:red;'>Cannot launch Observatory: {main_script} not found.</span>")
            return
        try:
            self.observatory_process = subprocess.Popen([sys.executable, main_script], cwd=script_dir)
            self.log_message(f"Launching Observatory Engine (cwd={script_dir})...")
            QTimer.singleShot(2000, self.safe_slot(self.check_observatory_health))
        except Exception as e:
            self.log_message(f"<span style='color:red;'>Failed to launch Observatory: {e}</span>")

    def check_observatory_health(self):
        proc = getattr(self, 'observatory_process', None)
        if proc is not None and proc.poll() is not None:
            self.log_message(
                f"<span style='color:red;'>Observatory process exited unexpectedly (code {proc.returncode}). "
                f"It is not running, so IPC commands (load project, vision waits, LLM triggers) will have no effect. "
                f"Check the console it was launched from for the actual error.</span>")
            return
        if not os.path.exists("vision_state.json"):
            self.log_message(
                "<span style='color:orange;'>Observatory hasn't written vision_state.json yet - "
                "it may still be starting up, or may not be running from this project's folder.</span>")

    def read_live_vision_state(self):
        try:
            if os.path.exists("vision_state.json"):
                with open("vision_state.json", "r") as f:
                    data = json.load(f)
                self.vision_tool_states = data.get("states", {})
                self.vision_tool_responses = data.get("responses", {})
                self.vision_tool_response_times = data.get("response_times", {})
                self.vision_captures = data.get("captures", {})
                self.sync_llm_call_displays()
                self.sync_capture_items()
        except OSError:
            # Transient and expected on Windows: Observatory swaps this file
            # via os.replace, and a read landing in that instant gets a
            # sharing violation. Harmless - the next tick (50ms) succeeds.
            # Measured at ~12% of reads under artificial hammering, far less
            # at the real 100ms write cadence.
            pass
        except Exception as e:
            # Anything else - notably JSONDecodeError - means genuinely bad
            # content rather than contention, and should be seen. Swallowing
            # this silently previously hid a missing method for a whole
            # session. Rate-limited so a persistent fault can't flood the
            # console 20x a second.
            now = time.time()
            if now - getattr(self, '_last_ipc_error_log', 0) > 5.0:
                self._last_ipc_error_log = now
                self.log_message(f"<span style='color:red;'>IPC read error: {e}</span>")

    def sync_llm_call_displays(self):
        """Mirror each LLM Call item's response from Observatory."""
        for item in self.all_canvas_items():
            if not isinstance(item, InteractiveLLMTextItem):
                continue
            if not item.source_tool or item.source_tool == "None":
                continue
            text = self.vision_tool_responses.get(
                item.source_tool, "Waiting for Observatory response...")
            if text != item.response:
                item.set_response(text)

    def sync_capture_items(self):
        """Mirror whatever the referenced Observatory Image Capture tool holds.

        Timestamp-compared so the image only reloads when Observatory
        actually captures something new, instead of re-reading the same PNG
        from disk on every 50ms IPC tick.
        """
        for item in self.all_canvas_items():
            if not isinstance(item, InteractiveCaptureItem):
                continue
            if not item.source_tool or item.source_tool == "None":
                continue
            info = self.vision_captures.get(item.source_tool)
            if not info:
                continue
            # The tool publishes both its raw capture and (once segmented)
            # its cutout, each with its own timestamp, so switching variant
            # picks a different image *and* a different freshness clock.
            if getattr(item, 'source_variant', "Captured") == "Segmented Cutout":
                path, when = info.get("cutout_path", ""), info.get("cutout_time", 0)
            else:
                path, when = info.get("path", ""), info.get("time", 0)
            if not path:
                continue
            if when > item.last_shown_time and item.load_capture(path):
                item.last_shown_time = when
                self.log_message(f"Capture '{item.item_name}' updated from "
                                 f"Observatory tool '{item.source_tool}'.")
                if self.active_item is item:
                    QTimer.singleShot(0, self.safe_slot(self.build_asset_properties, item))

    def clear_layout(self, layout):
        # Always take+deleteLater (never QFormLayout.removeRow, which deletes
        # widgets synchronously) so this is safe to call from inside a widget's
        # own signal handler, such as rebuilding this panel right after a
        # Browse button's own click finishes.
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self.clear_layout(item.layout())
                item.layout().deleteLater()

    def build_asset_properties(self, item):
        try:
            self.clear_layout(self.prop_form)
            self.prop_form.addRow(QLabel("<b>Asset Properties</b>"))

            if isinstance(item, InteractiveTextItem):
                txt = QLineEdit(item.toPlainText())
                txt.editingFinished.connect(lambda: item.setPlainText(txt.text()))
                self.prop_form.addRow("Text:", txt)

                spin_size = QSpinBox()
                spin_size.setRange(8, 200)
                spin_size.setValue(item.current_font_size)
                spin_size.valueChanged.connect(lambda v: self.apply_font_size(item, v))
                self.prop_form.addRow("Font Size:", spin_size)

                color_btn = self.build_color_picker_button(lambda: item.text_color, item.set_text_color)
                self.prop_form.addRow("Text Color:", color_btn)

                spin_scale = QSpinBox()
                spin_scale.setRange(10, 500)
                spin_scale.setSuffix(" %")
                spin_scale.setValue(int(item.scale_factor * 100))
                spin_scale.valueChanged.connect(lambda v: item.set_scale_factor(v / 100.0))
                self.prop_form.addRow("Size:", spin_scale)

                spin_rot = QSpinBox()
                spin_rot.setRange(0, 359)
                spin_rot.setValue(int(item.rotation()))
                spin_rot.valueChanged.connect(lambda v: item.setRotation(v))
                self.prop_form.addRow("Rotation:", spin_rot)

                chk_blink = QCheckBox("Blink while waiting")
                chk_blink.setChecked(item.is_blinking)
                chk_blink.toggled.connect(lambda v: setattr(item, 'is_blinking', v))
                self.prop_form.addRow("Blink:", chk_blink)

                spin_curve = QSpinBox()
                spin_curve.setRange(-3000, 3000)
                spin_curve.setSingleStep(10)
                spin_curve.setValue(int(item.curve_radius))
                spin_curve.valueChanged.connect(lambda v: item.set_curve_radius(v))
                self.prop_form.addRow("Curve Radius (0=straight):", spin_curve)

                self.build_animation_controls(item)
                self.build_lock_and_reset_controls(item, show_lock=True)
                self.build_output_canvas_selector(item)

            elif isinstance(item, InteractiveMediaItem):
                self.prop_form.addRow("File:", QLabel(os.path.basename(item.filepath)))

                spin_scale = QSpinBox()
                spin_scale.setRange(10, 400)
                spin_scale.setSuffix(" %")
                spin_scale.setValue(int(item.scale_factor * 100))
                spin_scale.valueChanged.connect(lambda v: item.set_scale_factor(v / 100.0))
                self.prop_form.addRow("Size:", spin_scale)

                spin_rot = QSpinBox()
                spin_rot.setRange(0, 359)
                spin_rot.setValue(int(item.rotation()))
                spin_rot.valueChanged.connect(lambda v: item.setRotation(v))
                self.prop_form.addRow("Rotation:", spin_rot)

                chk_blink = QCheckBox("Blink while waiting")
                chk_blink.setChecked(item.is_blinking)
                chk_blink.toggled.connect(lambda v: setattr(item, 'is_blinking', v))
                self.prop_form.addRow("Blink:", chk_blink)

                if item.media_type == "Video":
                    if item.media_player:
                        self.prop_form.addRow(QLabel("<hr><b>Video Playback</b>"))

                        duration_label = QLabel("Duration: ...")
                        self.prop_form.addRow(duration_label)

                        scrub = QSlider(Qt.Orientation.Horizontal)
                        scrub.setRange(0, max(0, item.media_player.duration()))
                        scrub.setValue(item.media_player.position())
                        # sliderMoved only fires on user drag, never on the
                        # programmatic setValue() below - avoids a feedback loop.
                        scrub.sliderMoved.connect(item.media_player.setPosition)
                        self.prop_form.addRow("Position:", scrub)

                        # Poll from our own timer instead of connecting to
                        # QMediaPlayer's positionChanged/durationChanged directly:
                        # those cross a thread boundary from the media backend,
                        # and wiring them straight into a widget living in this
                        # frequently rebuilt/deleted panel crashed reliably in
                        # testing. Parenting the timer to the slider means Qt
                        # cleans it up automatically once the panel is rebuilt.
                        poll_timer = QTimer(scrub)

                        def poll_video_position(player=item.media_player, slider=scrub, label=duration_label):
                            duration_ms = max(0, player.duration())
                            slider.setRange(0, duration_ms)
                            slider.setValue(player.position())
                            label.setText(f"Duration: {duration_ms / 1000.0:.2f} sec")

                        poll_timer.timeout.connect(self.safe_slot(poll_video_position))
                        poll_timer.start(250)

                        btn_reset = QPushButton("⏮️ Reset & Play from Start")
                        btn_reset.clicked.connect(item.play_from_start)
                        self.prop_form.addRow(btn_reset)

                        chk_hold = QCheckBox("Hold step until video finishes playing")
                        chk_hold.setChecked(item.time_hold)
                        chk_hold.toggled.connect(lambda v: setattr(item, 'time_hold', v))
                        self.prop_form.addRow("Time Hold:", chk_hold)
                    else:
                        self.prop_form.addRow(QLabel("<i>No video file loaded.</i>"))

                self.build_animation_controls(item)
                self.build_lock_and_reset_controls(item, show_lock=(item.media_type != "Video"))
                self.build_output_canvas_selector(item)

            elif isinstance(item, InteractiveShapeItem):
                self.prop_form.addRow("Shape:", QLabel(item.shape_type))

                if item.shape_type == "Custom SVG":
                    btn_browse_svg = QPushButton("Browse SVG...")

                    def browse_new_svg(i=item):
                        shape_lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shape_library")
                        path = self.prompt_open_file("Select SVG", "SVG Files (*.svg)", start_dir=shape_lib_dir)
                        if path:
                            i.set_shape_svg(path)
                            QTimer.singleShot(0, self.safe_slot(self.build_asset_properties, i))

                    btn_browse_svg.clicked.connect(self.safe_slot(browse_new_svg))
                    self.prop_form.addRow("File:", btn_browse_svg)
                    self.prop_form.addRow(
                        QLabel(os.path.basename(item.svg_path) if item.svg_path else "<i>No file selected</i>"))
                else:
                    spin_outline = QSpinBox()
                    spin_outline.setRange(0, 60)
                    spin_outline.setValue(item.outline_width)
                    spin_outline.valueChanged.connect(lambda v: item.set_outline_width(v))
                    self.prop_form.addRow("Outline Thickness:", spin_outline)

                    outline_color_btn = self.build_color_picker_button(lambda: item.outline_color,
                                                                        item.set_outline_color)
                    self.prop_form.addRow("Outline Color:", outline_color_btn)

                    chk_fill = QCheckBox("Filled")
                    chk_fill.setChecked(item.fill_enabled)
                    chk_fill.toggled.connect(lambda v: item.set_fill_enabled(v))
                    self.prop_form.addRow("Fill:", chk_fill)

                    fill_color_btn = self.build_color_picker_button(lambda: item.fill_color, item.set_fill_color)
                    self.prop_form.addRow("Fill Color:", fill_color_btn)

                spin_scale = QSpinBox()
                spin_scale.setRange(10, 500)
                spin_scale.setSuffix(" %")
                spin_scale.setValue(int(item.scale_factor * 100))
                spin_scale.valueChanged.connect(lambda v: item.set_scale_factor(v / 100.0))
                self.prop_form.addRow("Size:", spin_scale)

                spin_rot = QSpinBox()
                spin_rot.setRange(0, 359)
                spin_rot.setValue(int(item.rotation()))
                spin_rot.valueChanged.connect(lambda v: item.setRotation(v))
                self.prop_form.addRow("Rotation:", spin_rot)

                chk_blink = QCheckBox("Blink while waiting")
                chk_blink.setChecked(item.is_blinking)
                chk_blink.toggled.connect(lambda v: setattr(item, 'is_blinking', v))
                self.prop_form.addRow("Blink:", chk_blink)

                self.build_animation_controls(item)
                self.build_lock_and_reset_controls(item, show_lock=True)
                self.build_output_canvas_selector(item)

            elif isinstance(item, Interactive3DModelItem):
                lbl_file = QLabel(os.path.basename(item.filepath) if item.filepath else "<i>No model loaded</i>")
                self.prop_form.addRow("Model File:", lbl_file)

                lbl_stats = QLabel(f"{item.face_count()} faces" if item.face_count() else "—")
                self.prop_form.addRow("Geometry:", lbl_stats)

                btn_browse_model = QPushButton("Browse .stl / .step ...")

                def browse_model(i=item):
                    path = self.prompt_open_file(
                        "Select 3D Model",
                        "3D Models (*.stl *.step *.stp *.obj *.ply *.glb);;All Files (*)")
                    if not path:
                        return
                    self.log_message(f"Loading 3D model '{os.path.basename(path)}'...")

                    def done(error, p=path, it=i):
                        if error:
                            self.log_message(
                                f"<span style='color:red;'>3D model load failed: {error}</span>")
                        else:
                            self.log_message(
                                f"Loaded '{os.path.basename(p)}' ({it.face_count()} faces).")
                        if self.active_item is it:
                            QTimer.singleShot(0, self.safe_slot(self.build_asset_properties, it))

                    i.load_model_async(path, on_done=done)

                btn_browse_model.clicked.connect(self.safe_slot(browse_model))
                self.prop_form.addRow(btn_browse_model)

                self.prop_form.addRow(QLabel(
                    "<hr><b>3D Pose</b><br><i>Left-drag on the model to orbit - it pivots around "
                    "the point you grab. Mouse wheel zooms.</i>"))

                spin_rx = QDoubleSpinBox(); spin_rx.setRange(-3600, 3600); spin_rx.setSuffix(" °")
                spin_rx.setDecimals(1); spin_rx.setValue(item.rot_x)
                spin_rx.valueChanged.connect(lambda v: item.set_rotation_3d(rx=v))
                self.prop_form.addRow("Rotation X:", spin_rx)

                spin_ry = QDoubleSpinBox(); spin_ry.setRange(-3600, 3600); spin_ry.setSuffix(" °")
                spin_ry.setDecimals(1); spin_ry.setValue(item.rot_y)
                spin_ry.valueChanged.connect(lambda v: item.set_rotation_3d(ry=v))
                self.prop_form.addRow("Rotation Y:", spin_ry)

                spin_rz = QDoubleSpinBox(); spin_rz.setRange(-3600, 3600); spin_rz.setSuffix(" °")
                spin_rz.setDecimals(1); spin_rz.setValue(item.rot_z)
                spin_rz.valueChanged.connect(lambda v: item.set_rotation_3d(rz=v))
                self.prop_form.addRow("Rotation Z:", spin_rz)

                spin_tx = QDoubleSpinBox(); spin_tx.setRange(-5000, 5000); spin_tx.setDecimals(1)
                spin_tx.setValue(item.model_tx)
                spin_tx.valueChanged.connect(lambda v: item.set_model_translation(tx=v))
                self.prop_form.addRow("Translate X:", spin_tx)

                spin_ty = QDoubleSpinBox(); spin_ty.setRange(-5000, 5000); spin_ty.setDecimals(1)
                spin_ty.setValue(item.model_ty)
                spin_ty.valueChanged.connect(lambda v: item.set_model_translation(ty=v))
                self.prop_form.addRow("Translate Y:", spin_ty)

                spin_mscale = QDoubleSpinBox(); spin_mscale.setRange(0.05, 20.0)
                spin_mscale.setSingleStep(0.1); spin_mscale.setDecimals(2)
                spin_mscale.setValue(item.model_scale)
                spin_mscale.valueChanged.connect(lambda v: item.set_model_scale(v))
                self.prop_form.addRow("Model Scale:", spin_mscale)

                # Live readout of the full pose. Polled rather than signalled
                # because dragging on the canvas changes these values without
                # going through the spinboxes.
                lbl_pose = QLabel()
                lbl_pose.setStyleSheet("color: #2ecc71; font-family: Consolas;")
                self.prop_form.addRow("Live Pose:", lbl_pose)

                pose_timer = QTimer(lbl_pose)

                def refresh_pose(it=item, lbl=lbl_pose,
                                 sx=spin_rx, sy=spin_ry, sz=spin_rz,
                                 stx=spin_tx, sty=spin_ty):
                    cx, cy = it.canvas_offset()
                    lbl.setText(
                        f"Canvas  X:{cx:8.1f}  Y:{cy:8.1f}   (from reference)\n"
                        f"Model   X:{it.model_tx:8.1f}  Y:{it.model_ty:8.1f}  Z:{0.0:8.1f}\n"
                        f"RX:{it.rot_x % 360:7.1f}° RY:{it.rot_y % 360:6.1f}° RZ:{it.rot_z % 360:6.1f}°\n"
                        f"Scale: {it.model_scale:.2f}x")
                    # Keep the spinboxes in step with canvas dragging without
                    # re-triggering their own valueChanged handlers.
                    for spin, val in ((sx, it.rot_x), (sy, it.rot_y), (sz, it.rot_z),
                                      (stx, it.model_tx), (sty, it.model_ty)):
                        if abs(spin.value() - val) > 0.05:
                            spin.blockSignals(True)
                            spin.setValue(val)
                            spin.blockSignals(False)

                pose_timer.timeout.connect(self.safe_slot(refresh_pose))
                pose_timer.start(150)
                refresh_pose()

                self.prop_form.addRow(QLabel(
                    "<i>Ctrl or Shift + drag moves the whole viewport box on the canvas. "
                    "Canvas X/Y above is measured from the reference point.</i>"))

                btn_set_home = QPushButton("⌖ Set Reference Point Here")
                btn_set_home.clicked.connect(
                    self.safe_slot(lambda: (item.set_home_position(),
                                            self.log_message("3D model reference point set to current position."))))
                self.prop_form.addRow(btn_set_home)

                btn_reset_view = QPushButton("↺ Reset 3D View + Position")
                btn_reset_view.clicked.connect(self.safe_slot(lambda: item.reset_view()))
                self.prop_form.addRow(btn_reset_view)

                color_btn = self.build_color_picker_button(
                    lambda: item.model_color,
                    lambda c: (setattr(item, 'model_color', c), item.update()))
                self.prop_form.addRow("Model Color:", color_btn)

                spin_vw = QSpinBox(); spin_vw.setRange(200, 2000)
                spin_vw.setValue(int(item.rect().width()))
                spin_vw.valueChanged.connect(lambda v: item.resize_by_drag(v, item.rect().height()))
                self.prop_form.addRow("Viewport Width:", spin_vw)

                spin_vh = QSpinBox(); spin_vh.setRange(150, 2000)
                spin_vh.setValue(int(item.rect().height()))
                spin_vh.valueChanged.connect(lambda v: item.resize_by_drag(item.rect().width(), v))
                self.prop_form.addRow("Viewport Height:", spin_vh)

                chk_blink = QCheckBox("Blink while waiting")
                chk_blink.setChecked(item.is_blinking)
                chk_blink.toggled.connect(lambda v: setattr(item, 'is_blinking', v))
                self.prop_form.addRow("Blink:", chk_blink)

                self.build_animation_controls(item)
                self.build_lock_and_reset_controls(item, show_lock=True)
                self.build_output_canvas_selector(item)

            elif isinstance(item, InteractiveCaptureItem):
                name_edit = QLineEdit(item.item_name)
                name_edit.editingFinished.connect(
                    lambda: item.set_item_name(name_edit.text().strip() or "Capture"))
                self.prop_form.addRow("Label:", name_edit)

                self.prop_form.addRow(QLabel(
                    "<i>Shows the still held by an Observatory <b>Image Capture</b> tool. "
                    "The capture itself, its ROI and its camera are configured there.</i>"))

                file_lay = QHBoxLayout()
                lbl_obs = QLineEdit(os.path.basename(item.observatory_file)
                                    if item.observatory_file else "None")
                lbl_obs.setReadOnly(True)
                btn_obs_file = QPushButton("Browse")

                def browse_obs(i=item, lbl=lbl_obs):
                    path = self.prompt_open_file("Select Observatory Project", "JSON Files (*.json)")
                    if path:
                        i.observatory_file = path
                        lbl.setText(os.path.basename(path))
                        QTimer.singleShot(0, self.safe_slot(self.build_asset_properties, i))

                btn_obs_file.clicked.connect(self.safe_slot(browse_obs))
                file_lay.addWidget(lbl_obs)
                file_lay.addWidget(btn_obs_file)
                self.prop_form.addRow("Observatory File:", file_lay)

                cb_source = QComboBox()
                tools = (self.parse_observatory_file_silent(item.observatory_file)
                         if item.observatory_file and os.path.exists(item.observatory_file)
                         else ["None"])
                cb_source.addItems(tools)
                cb_source.setCurrentText(item.source_tool if item.source_tool in tools else "None")
                cb_source.currentTextChanged.connect(lambda v: setattr(item, "source_tool", v))
                self.prop_form.addRow("Capture Tool:", cb_source)

                cb_variant = QComboBox()
                cb_variant.addItems(["Captured", "Segmented Cutout"])
                cb_variant.setCurrentText(getattr(item, 'source_variant', "Captured"))

                def on_variant_changed(v, i=item):
                    i.source_variant = v
                    # Force the next IPC tick to reload. The newly chosen
                    # variant's timestamp is usually OLDER than what we last
                    # displayed, so without resetting this the freshness
                    # check would reject it and the image would never swap.
                    i.last_shown_time = 0.0

                cb_variant.currentTextChanged.connect(on_variant_changed)
                self.prop_form.addRow("Show:", cb_variant)
                self.prop_form.addRow(QLabel(
                    "<i><b>Captured</b> is the raw still. <b>Segmented Cutout</b> is the "
                    "transparent-background version - it only appears once that tool has "
                    "actually been segmented in Observatory.</i>"))

                img_path_field = QLineEdit(item.image_path or "")
                img_path_field.setReadOnly(True)
                img_path_field.setPlaceholderText("Nothing captured yet")
                img_path_field.setToolTip(item.image_path or "")
                img_path_field.setCursorPosition(0)
                self.prop_form.addRow("Image:", img_path_field)

                spin_rot = QSpinBox(); spin_rot.setRange(0, 359)
                spin_rot.setValue(int(item.rotation()))
                spin_rot.valueChanged.connect(lambda v: item.setRotation(v))
                self.prop_form.addRow("Rotation:", spin_rot)

                chk_blink = QCheckBox("Blink while waiting")
                chk_blink.setChecked(item.is_blinking)
                chk_blink.toggled.connect(lambda v: setattr(item, "is_blinking", v))
                self.prop_form.addRow("Blink:", chk_blink)

                self.build_animation_controls(item)
                self.build_lock_and_reset_controls(item, show_lock=True)
                self.build_output_canvas_selector(item)

            elif isinstance(item, InteractiveHTMLItem):
                url_edit = QLineEdit(item.url)
                url_edit.setPlaceholderText("https://... or a local .html file path")
                url_edit.editingFinished.connect(lambda: item.load_url(url_edit.text().strip()))
                self.prop_form.addRow("URL / File:", url_edit)

                btn_browse_html = QPushButton("Browse Local File...")

                def browse_html(i=item, e=url_edit):
                    path = self.prompt_open_file("Select HTML File", "HTML Files (*.html *.htm)")
                    if path:
                        e.setText(path)
                        i.load_url(path)

                btn_browse_html.clicked.connect(self.safe_slot(browse_html))
                self.prop_form.addRow(btn_browse_html)

                btn_reload = QPushButton("↻ Reload")
                btn_reload.clicked.connect(lambda: item.load_url(item.url))
                self.prop_form.addRow(btn_reload)

                spin_w = QSpinBox()
                spin_w.setRange(160, 2000)
                spin_w.setValue(item.base_width)
                spin_w.valueChanged.connect(lambda v: item.resize_by_drag(v, item.base_height))
                self.prop_form.addRow("Width:", spin_w)

                spin_h = QSpinBox()
                spin_h.setRange(120, 2000)
                spin_h.setValue(item.base_height)
                spin_h.valueChanged.connect(lambda v: item.resize_by_drag(item.base_width, v))
                self.prop_form.addRow("Height:", spin_h)
                self.build_output_canvas_selector(item)

            elif isinstance(item, InteractiveLLMTextItem):
                name_edit = QLineEdit(item.tool_name)
                name_edit.editingFinished.connect(lambda: item.set_tool_name(name_edit.text().strip() or "LLM Call"))
                self.prop_form.addRow("Tool Name:", name_edit)

                file_lay = QHBoxLayout()
                lbl_file = QLineEdit(os.path.basename(item.observatory_file) if item.observatory_file else "None")
                lbl_file.setReadOnly(True)
                btn_file = QPushButton("Browse")

                def browse_llm_source():
                    # No parameters: QPushButton.clicked emits clicked(bool checked=False),
                    # and PyQt6 will pass that bool into the first positional parameter of
                    # whatever slot it's connected to. A slot shaped like (item=item) looks
                    # like it accepts one positional arg, so PyQt6 was overwriting the
                    # intended `item` object with the click's bool value entirely silently -
                    # `item.observatory_file = path` then became `False.observatory_file = path`,
                    # raising AttributeError inside Qt's own signal dispatch, which this
                    # Qt/PyQt6 build escalates to a hard qFatal() abort instead of a normal
                    # traceback. `item` is still captured correctly via the closure alone.
                    path = self.prompt_open_file("Select Observatory Project", "JSON Files (*.json)")
                    if path:
                        item.observatory_file = path
                        QTimer.singleShot(0, self.safe_slot(self.build_asset_properties, item))

                btn_file.clicked.connect(browse_llm_source)
                file_lay.addWidget(lbl_file)
                file_lay.addWidget(btn_file)
                self.prop_form.addRow("Observatory File:", file_lay)

                cb_source = QComboBox()
                if item.observatory_file and os.path.exists(item.observatory_file):
                    tools = self.parse_observatory_file_silent(item.observatory_file)
                else:
                    tools = ["None"]
                cb_source.addItems(tools)
                cb_source.setCurrentText(item.source_tool if item.source_tool in tools else "None")
                cb_source.currentTextChanged.connect(lambda v: setattr(item, 'source_tool', v))
                self.prop_form.addRow("LLM Tool Target:", cb_source)

                prompt_edit = QTextEdit(item.prompt)
                prompt_edit.setMaximumHeight(80)
                prompt_edit.textChanged.connect(lambda: setattr(item, 'prompt', prompt_edit.toPlainText()))
                self.prop_form.addRow("LLM Prompt:", prompt_edit)

                spin_font = QSpinBox()
                spin_font.setRange(8, 60)
                spin_font.setValue(item.current_font_size)
                spin_font.valueChanged.connect(lambda v: item.set_font_size(v))
                self.prop_form.addRow("Response Font Size:", spin_font)

                color_btn = self.build_color_picker_button(lambda: item.text_color, item.set_text_color)
                self.prop_form.addRow("Response Text Color:", color_btn)

                chk_hold = QCheckBox("Hold step until response arrives (60s failsafe)")
                chk_hold.setChecked(item.hold_for_response)
                chk_hold.toggled.connect(lambda v: setattr(item, 'hold_for_response', v))
                self.prop_form.addRow("Hold for Response:", chk_hold)
                self.build_output_canvas_selector(item)

                spin_w = QSpinBox()
                spin_w.setRange(220, 1600)
                spin_w.setValue(int(item.rect().width()))
                spin_w.valueChanged.connect(lambda v: item.update_size(v, item.rect().height()))
                self.prop_form.addRow("Box Width:", spin_w)

                spin_h = QSpinBox()
                spin_h.setRange(120, 1200)
                spin_h.setValue(int(item.rect().height()))
                spin_h.valueChanged.connect(lambda v: item.update_size(item.rect().width(), v))
                self.prop_form.addRow("Box Height:", spin_h)

                spin_rot = QSpinBox()
                spin_rot.setRange(0, 359)
                spin_rot.setValue(int(item.rotation()))
                spin_rot.valueChanged.connect(lambda v: item.setRotation(v))
                self.prop_form.addRow("Rotation:", spin_rot)

            if hasattr(item, 'trigger'):
                self.prop_form.addRow(QLabel("<hr><b>Confirmation Settings</b>"))
                cb_type = QComboBox()
                cb_type.addItems(["Vision Variable", "Line"])
                cb_type.setCurrentText(item.trigger.wait_type)

                def on_trigger_type_changed(v):
                    item.trigger.wait_type = v
                    QTimer.singleShot(0, self.safe_slot(self.build_asset_properties, item))

                cb_type.currentTextChanged.connect(on_trigger_type_changed)
                self.prop_form.addRow("Trigger Type:", cb_type)

                if item.trigger.wait_type == "Line":
                    self.prop_form.addRow(QLabel(
                        "<i>Executes automatically as the sequence flow reaches this line. No condition needed.</i>"))

                elif item.trigger.wait_type == "Vision Variable":
                    file_lay = QHBoxLayout()
                    lbl_file = QLineEdit(
                        os.path.basename(item.trigger.observatory_file) if item.trigger.observatory_file else "None")
                    lbl_file.setReadOnly(True)
                    btn_file = QPushButton("Browse")
                    btn_file.clicked.connect(lambda: self.browse_file_for_item(item, lbl_file))
                    file_lay.addWidget(lbl_file)
                    file_lay.addWidget(btn_file)
                    self.prop_form.addRow("Obs File:", file_lay)

                    cb_target = QComboBox()
                    if item.trigger.observatory_file and os.path.exists(item.trigger.observatory_file):
                        tools = self.parse_observatory_file_silent(item.trigger.observatory_file)
                    else:
                        tools = ["None"]

                    cb_target.addItems(tools)
                    if item.trigger.vision_target in tools:
                        cb_target.setCurrentText(item.trigger.vision_target)
                    else:
                        cb_target.setCurrentText("None")

                    cb_target.currentTextChanged.connect(lambda v: setattr(item.trigger, 'vision_target', v))
                    self.prop_form.addRow("Vision Target:", cb_target)

                    cb_cond = QComboBox()
                    cb_cond.addItems(["Is True", "Is False", "Becomes True (Rising Edge)", "Becomes False (Falling Edge)"])
                    cb_cond.setCurrentText(item.trigger.condition)
                    cb_cond.currentTextChanged.connect(lambda v: setattr(item.trigger, 'condition', v))
                    self.prop_form.addRow("Condition:", cb_cond)

        except Exception as e:
            print(f"UI BUILDER ERROR: {e}")

    def build_node_properties(self, asset):
        try:
            self.clear_layout(self.node_prop_form)

            if isinstance(asset, FlowControlData):
                self.node_prop_form.addRow(QLabel("<b>Flow Control Settings</b>"))
                cb_action = QComboBox()
                cb_action.addItems(["End Program", "Loop Continuously", "Jump to Step Line"])
                cb_action.setCurrentText(asset.action)
                cb_action.currentTextChanged.connect(lambda v: setattr(asset, 'action', v))
                cb_action.currentTextChanged.connect(lambda v: self.update_tree_labels())
                self.node_prop_form.addRow("End Action:", cb_action)

                spin_target = QSpinBox()
                spin_target.setRange(1, 100)
                spin_target.setValue(asset.target_step)
                spin_target.valueChanged.connect(lambda v: setattr(asset, 'target_step', v))
                spin_target.valueChanged.connect(lambda v: self.update_tree_labels())
                self.node_prop_form.addRow("Target Line (# column):", spin_target)

            elif isinstance(asset, TimerData):
                self.node_prop_form.addRow(QLabel("<b>Timer Settings</b>"))
                spin_dur = QDoubleSpinBox()
                spin_dur.setRange(0.1, 3600.0)
                spin_dur.setValue(asset.duration)
                spin_dur.valueChanged.connect(lambda v: setattr(asset, 'duration', v))
                spin_dur.valueChanged.connect(lambda v: self.update_tree_labels())
                self.node_prop_form.addRow("Duration (sec):", spin_dur)

            elif isinstance(asset, ObservatoryNodeData):
                self.node_prop_form.addRow(QLabel("<b>Observatory Auto-Load Node</b>"))
                file_lay = QHBoxLayout()
                lbl_file = QLineEdit(os.path.basename(asset.filepath) if asset.filepath else "None")
                lbl_file.setReadOnly(True)
                btn_file = QPushButton("Browse")

                def browse():
                    path = self.prompt_open_file("Select Observatory Project", "JSON Files (*.json)")
                    if path:
                        asset.filepath = path
                        lbl_file.setText(os.path.basename(path))
                        self.update_tree_labels()

                btn_file.clicked.connect(browse)
                file_lay.addWidget(lbl_file)
                file_lay.addWidget(btn_file)
                self.node_prop_form.addRow("Obs File:", file_lay)

            elif isinstance(asset, ClearCanvasData):
                self.node_prop_form.addRow(QLabel("<i>Executes immediately on step start.</i>"))
                cb_target = QComboBox()
                cb_target.addItem("All Canvases", -1)
                for i in range(len(self.canvases)):
                    cb_target.addItem(f"Canvas {i + 1}", i)
                target_idx = cb_target.findData(getattr(asset, 'output_canvas', -1))
                cb_target.setCurrentIndex(target_idx if target_idx >= 0 else 0)
                cb_target.currentIndexChanged.connect(
                    lambda _, a=asset, c=cb_target: setattr(a, 'output_canvas', c.currentData()))
                cb_target.currentIndexChanged.connect(lambda _: self.update_tree_labels())
                self.node_prop_form.addRow("Clear Which:", cb_target)

            elif isinstance(asset, VisionWaitData):
                self.node_prop_form.addRow(QLabel("<b>Vision Wait Settings</b>"))
                file_lay = QHBoxLayout()
                lbl_file = QLineEdit(os.path.basename(asset.observatory_file) if asset.observatory_file else "None")
                lbl_file.setReadOnly(True)
                btn_file = QPushButton("Browse")

                def browse_wait():
                    path = self.prompt_open_file("Select Observatory Project", "JSON Files (*.json)")
                    if path:
                        asset.observatory_file = path
                        lbl_file.setText(os.path.basename(path))
                        self.update_tree_labels()
                        QTimer.singleShot(0, self.safe_slot(self.build_node_properties, asset))

                btn_file.clicked.connect(browse_wait)
                file_lay.addWidget(lbl_file)
                file_lay.addWidget(btn_file)
                self.node_prop_form.addRow("Obs File:", file_lay)

                cb_target = QComboBox()
                tools = self.parse_observatory_file_silent(
                    asset.observatory_file) if asset.observatory_file and os.path.exists(asset.observatory_file) else [
                    "None"]
                cb_target.addItems(tools)
                cb_target.setCurrentText(asset.vision_target if asset.vision_target in tools else "None")
                cb_target.currentTextChanged.connect(lambda v: setattr(asset, 'vision_target', v))
                cb_target.currentTextChanged.connect(lambda v: self.update_tree_labels())
                self.node_prop_form.addRow("Vision Target:", cb_target)

                cb_cond = QComboBox()
                cb_cond.addItems(["Is True", "Is False", "Becomes True (Rising Edge)", "Becomes False (Falling Edge)"])
                cb_cond.setCurrentText(asset.condition)
                cb_cond.currentTextChanged.connect(lambda v: setattr(asset, 'condition', v))
                cb_cond.currentTextChanged.connect(lambda v: self.update_tree_labels())
                self.node_prop_form.addRow("Condition:", cb_cond)

            elif isinstance(asset, UserWaitData):
                self.node_prop_form.addRow(QLabel("<b>User Wait Settings</b>"))
                self.node_prop_form.addRow(QLabel(
                    "<i>Blocks this step until the Back/Forward buttons above the Run Mode "
                    "output panel are clicked.</i>"))

                cb_fwd = QComboBox()
                cb_fwd.addItems(["Next Step", "Previous Step", "Jump to Step Line"])
                cb_fwd.setCurrentText(asset.forward_action)
                cb_fwd.currentTextChanged.connect(lambda v: setattr(asset, 'forward_action', v))
                cb_fwd.currentTextChanged.connect(lambda v: self.update_tree_labels())
                self.node_prop_form.addRow("Forward Button:", cb_fwd)

                spin_fwd_target = QSpinBox()
                spin_fwd_target.setRange(1, 200)
                spin_fwd_target.setValue(asset.forward_target_step)
                spin_fwd_target.valueChanged.connect(lambda v: setattr(asset, 'forward_target_step', v))
                spin_fwd_target.valueChanged.connect(lambda v: self.update_tree_labels())
                self.node_prop_form.addRow("Forward Target Line:", spin_fwd_target)

                cb_back = QComboBox()
                cb_back.addItems(["Next Step", "Previous Step", "Jump to Step Line"])
                cb_back.setCurrentText(asset.backward_action)
                cb_back.currentTextChanged.connect(lambda v: setattr(asset, 'backward_action', v))
                cb_back.currentTextChanged.connect(lambda v: self.update_tree_labels())
                self.node_prop_form.addRow("Back Button:", cb_back)

                spin_back_target = QSpinBox()
                spin_back_target.setRange(1, 200)
                spin_back_target.setValue(asset.backward_target_step)
                spin_back_target.valueChanged.connect(lambda v: setattr(asset, 'backward_target_step', v))
                spin_back_target.valueChanged.connect(lambda v: self.update_tree_labels())
                self.node_prop_form.addRow("Back Target Line:", spin_back_target)

        except Exception as e:
            print(f"NODE UI ERROR: {e}")

    def apply_font_size(self, item, size):
        item.current_font_size = size
        item.setFont(QFont("Arial", size))

    def build_color_picker_button(self, get_color, on_pick):
        btn = QPushButton()
        btn.setFixedWidth(60)

        def refresh():
            btn.setStyleSheet(f"background-color: {get_color().name()}; border: 1px solid #888;")

        def pick():
            color = QColorDialog.getColor(get_color(), self, "Select Text Color")
            if color.isValid():
                on_pick(color)
                refresh()

        btn.clicked.connect(pick)
        refresh()
        return btn

    def build_animation_controls(self, item):
        self.prop_form.addRow(QLabel("<hr><b>Animation</b>"))

        cb_anim = QComboBox()
        cb_anim.addItems(["None", "Fade In", "Slide In", "Pulse", "Pan"])
        cb_anim.setCurrentText(item.animation_type)
        cb_anim.currentTextChanged.connect(lambda v: item.set_animation_type(v))
        self.prop_form.addRow("Type:", cb_anim)

        cb_dir = QComboBox()
        cb_dir.addItems(["Left", "Right", "Top", "Bottom"])
        cb_dir.setCurrentText(item.animation_direction)
        cb_dir.currentTextChanged.connect(lambda v: item.set_animation_direction(v))
        self.prop_form.addRow("Direction:", cb_dir)

        spin_dur = QDoubleSpinBox()
        spin_dur.setRange(0.1, 10.0)
        spin_dur.setSingleStep(0.1)
        spin_dur.setSuffix(" sec")
        spin_dur.setValue(item.animation_duration)
        spin_dur.valueChanged.connect(lambda v: item.set_animation_duration(v))
        self.prop_form.addRow("Duration:", spin_dur)

    def build_lock_and_reset_controls(self, item, show_lock=True):
        self.prop_form.addRow(QLabel("<hr><b>Transform</b>"))

        if show_lock:
            chk_lock = QCheckBox("Lock Aspect Ratio")
            chk_lock.setChecked(item.lock_aspect_ratio)
            chk_lock.toggled.connect(lambda v: item.set_lock_aspect_ratio(v))
            self.prop_form.addRow("Lock:", chk_lock)

        btn_reset = QPushButton("↺ Reset to Default")

        def do_reset(i=item):
            i.reset_to_default()
            QTimer.singleShot(0, self.safe_slot(self.build_asset_properties, i))

        btn_reset.clicked.connect(self.safe_slot(do_reset))
        self.prop_form.addRow(btn_reset)

    def load_scale_state(self, item, child_data):
        item.lock_aspect_ratio = child_data.get('lock_aspect_ratio', True)
        if item.lock_aspect_ratio:
            item.set_scale_factor(child_data.get('scale_factor', 1.0))
        else:
            item.set_scale_xy(child_data.get('scale_x', 1.0), child_data.get('scale_y', 1.0))

    def browse_file_for_item(self, item, line_edit):
        filepath = self.prompt_open_file("Select Observatory Project", "JSON Files (*.json)")
        if filepath:
            item.trigger.observatory_file = filepath
            line_edit.setText(os.path.basename(filepath))
            QTimer.singleShot(0, self.safe_slot(self.build_asset_properties, item))

    def hide_all_canvas_items(self, canvas_index=None):
        """Hide assets. canvas_index=None (or -1) clears every canvas;
        otherwise only the assets living on that one canvas are hidden."""
        # Only touch top-level items: hiding a parent already cascades to its
        # children, but showing a parent later does NOT un-hide children whose
        # own visibility flag was explicitly cleared here.
        if canvas_index is None or canvas_index < 0:
            targets = self.all_canvas_items()
        elif 0 <= canvas_index < len(self.canvases):
            targets = self.canvases[canvas_index].scene.items()
        else:
            targets = []
        for item in targets:
            if item.parentItem() is None:
                item.setVisible(False)
                if isinstance(item, InteractiveMediaItem):
                    item.stop_video()

    def enter_design_mode(self):
        self.is_running = False
        self.in_design_mode = True
        for btn in [self.btn_restart, self.btn_play, self.btn_stop]: btn.setVisible(False)
        self.btn_design_mode.setStyleSheet("background-color: #2ecc71; font-weight: bold; color: black;")
        self.btn_run_mode.setStyleSheet("background-color: #3C3F41; color: white;")
        for item in self.all_canvas_items(): item.setVisible(True)
        self.center_stack.setCurrentWidget(self.step_tree)
        self.insert_tools_container.setVisible(True)
        self.ai_group.setVisible(True)
        self.btn_del_line.setVisible(True)
        self.run_sequence_view.setVisible(False)
        self.btn_user_wait_back.setEnabled(False)
        self.btn_user_wait_forward.setEnabled(False)
        self.log_message("Authoring: Design Mode active.")

    def enter_run_mode(self):
        self.in_design_mode = False
        self.btn_design_mode.setStyleSheet("background-color: #3C3F41; color: white;")
        self.btn_run_mode.setStyleSheet("background-color: #e74c3c; font-weight: bold; color: white;")
        for btn in [self.btn_restart, self.btn_play, self.btn_stop]: btn.setVisible(True)
        self.hide_all_canvas_items()
        self.center_stack.setCurrentWidget(self.run_view_container)
        self.run_preview_view.fitInView(self.run_preview_view.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        # Inserting/editing the sequence or asking the AI assistant mid-run
        # doesn't make sense, so that whole panel gives way to a read-only
        # mirror of the sequence with the currently-executing step
        # highlighted instead.
        self.insert_tools_container.setVisible(False)
        self.ai_group.setVisible(False)
        self.btn_del_line.setVisible(False)
        self.run_sequence_view.setVisible(True)
        self.build_run_sequence_mirror()
        self.btn_user_wait_back.setEnabled(False)
        self.btn_user_wait_forward.setEnabled(False)

        self.execution_sequence = self.build_execution_sequence()
        self.wait_state = "IDLE"

        sel = self.step_tree.selectedItems()
        self.pc = max(0,
                      self.step_tree.indexOfTopLevelItem(sel[0].parent() if sel[0].parent() else sel[0])) if sel else 0

        self.is_running = False
        self.update_playback_ui()
        self.highlight_current_run_step()
        self.log_message(f"Run Mode ready. Paused at Step {self.pc + 1}.")

    def play_sequence(self):
        self.is_running = True
        self.log_message(f"Playing from step {self.pc + 1}...")
        self.update_playback_ui()

    def stop_sequence(self):
        self.is_running = False
        for item in self.all_canvas_items():
            if isinstance(item, InteractiveMediaItem):
                item.stop_video()
        self.log_message("Execution Paused/Stopped.")
        self.update_playback_ui()

    def closeEvent(self, event):
        # Asset types owning backend resources (a Chromium renderer process,
        # a media playback pipeline) get told to stop here, before Qt starts
        # destroying the widgets holding them.
        #
        # Deliberately NOT calling deleteLater() on web_view:
        # QGraphicsProxyWidget.setWidget() TAKES OWNERSHIP, so the proxy
        # already deletes it - queueing a second delete here is a
        # double-free and a native crash in its own right.
        #
        # The whole body is guarded because closeEvent is invoked through
        # Qt's meta-object system: an exception escaping it doesn't produce
        # a normal traceback, it gets escalated to qFatal()/
        # STATUS_STACK_BUFFER_OVERRUN and takes the process down. Failing to
        # tidy up on the way out is never worth crashing over.
        try:
            self.master_clock.stop()
            self.ipc_timer.stop()
            for item in self.all_canvas_items():
                if isinstance(item, InteractiveHTMLItem):
                    item.web_view.stop()
                elif isinstance(item, InteractiveMediaItem):
                    item.stop_video()

            # Background workers (model loading, segmentation) hold Python
            # objects that Qt is about to destroy. Letting a thread still be
            # running when the interpreter tears down its target is a
            # crash-on-exit; wait briefly for each instead. Bounded so a
            # genuinely stuck worker can't block quitting - the process is
            # ending anyway.
            for item in self.all_canvas_items():
                for attr in ('_loaders', '_seg_workers'):
                    for worker in list(getattr(item, attr, []) or []):
                        try:
                            if worker.isRunning():
                                worker.wait(2000)
                        except Exception:
                            pass
        except Exception as e:
            print(f"Cleanup on close failed (continuing to close anyway): {e}")
        super().closeEvent(event)

    def restart_sequence(self):
        self.pc = 0
        self.wait_state = "IDLE"
        self.is_running = True
        for item in self.all_canvas_items():
            if isinstance(item, InteractiveMediaItem):
                item.stop_video()
        self.log_message("Restarting sequence from Step 1...")
        self.update_playback_ui()

    def update_playback_ui(self):
        if self.is_running:
            self.btn_play.setStyleSheet("background-color: #2ecc71; color: black; font-weight: bold;")
            self.btn_stop.setStyleSheet("background-color: #3C3F41; color: white;")
        else:
            self.btn_play.setStyleSheet("background-color: #3C3F41; color: white;")
            self.btn_stop.setStyleSheet("background-color: #e74c3c; color: white; font-weight: bold;")

    def vision_condition_met(self, condition, current_state, prev_state):
        if condition == "Is True": return current_state
        if condition == "Is False": return not current_state
        if condition == "Becomes True (Rising Edge)": return current_state and not prev_state
        if condition == "Becomes False (Falling Edge)": return not current_state and prev_state
        return False

    def check_llm_response_freshness(self, item):
        # True means "stop waiting" - either a genuinely fresh response (one
        # whose timestamp is newer than when THIS trigger was sent, so it
        # can't be a stale answer left over from a previous loop iteration)
        # has arrived, or the failsafe timeout gave up waiting.
        if not item.response_pending:
            return True
        response_time = self.vision_tool_response_times.get(item.source_tool, 0)
        if response_time > item.trigger_sent_time:
            item.response_pending = False
            return True
        if time.time() - item.trigger_sent_time > 60:
            if not item.failsafe_triggered:
                item.failsafe_triggered = True
                item.set_response("⚠️ ALARM: No response within 60s - proceeding anyway.")
                self.log_message(
                    f"<span style='color:red;'><b>ALARM:</b> LLM Call '{item.tool_name}' "
                    f"('{item.source_tool}') did not respond within 60 seconds. Proceeding without a fresh response.</span>")
            item.response_pending = False
            return True
        return False

    def build_execution_sequence(self):
        seq = []
        for i in range(self.step_tree.topLevelItemCount()):
            top_node = self.step_tree.topLevelItem(i)
            step_data = {"assets": [], "flow_node": None, "clear_node": None, "timer_node": None, "obs_node": None,
                         "wait_nodes": [], "user_wait_node": None}

            for j in range(top_node.childCount()):
                child = top_node.child(j)
                asset = child.data(1, Qt.ItemDataRole.UserRole)
                if isinstance(asset, FlowControlData):
                    step_data["flow_node"] = asset
                elif isinstance(asset, ClearCanvasData):
                    step_data["clear_node"] = asset
                elif isinstance(asset, TimerData):
                    step_data["timer_node"] = asset
                elif isinstance(asset, ObservatoryNodeData):
                    step_data["obs_node"] = asset
                elif isinstance(asset, VisionWaitData):
                    step_data["wait_nodes"].append(asset)
                elif isinstance(asset, UserWaitData):
                    step_data["user_wait_node"] = asset
                elif asset:
                    step_data["assets"].append(asset)
            seq.append(step_data)
        return seq

    def process_execution_engine(self):
        # This runs on a 100ms QTimer. An uncaught exception escaping a
        # Qt-invoked slot gets escalated to a hard process abort by this
        # PyQt6 build instead of a catchable traceback, so every tick must
        # be exception-safe rather than letting bugs here take the whole
        # app down.
        try:
            self._process_execution_engine_body()
        except Exception as e:
            self.log_message(f"<span style='color:red;'>Engine Error: {e}</span>")
            self.is_running = False

    def _process_execution_engine_body(self):
        self.blink_state = not self.blink_state
        if not self.is_running: return

        if self.pc >= len(self.execution_sequence):
            self.enter_design_mode()
            self.log_message("Program complete. Returning to Design Mode.")
            return

        step = self.execution_sequence[self.pc]

        if self.wait_state == "IDLE":
            self.user_wait_conditions_met = False
            if step["clear_node"]:
                self.hide_all_canvas_items(getattr(step["clear_node"], 'output_canvas', -1))

            if step["obs_node"] and step["obs_node"].filepath: self.emit_observatory_command(step["obs_node"].filepath)

            for asset in step["assets"]:
                asset.setVisible(True)
                if hasattr(asset, 'trigger') and asset.trigger.observatory_file:
                    self.emit_observatory_command(asset.trigger.observatory_file)
                if isinstance(asset, InteractiveLLMTextItem):
                    self.emit_llm_trigger_command(asset)
                if isinstance(asset, InteractiveMediaItem) and asset.media_type == "Video":
                    asset.play_from_start()

            for w_node in step["wait_nodes"]:
                if w_node.observatory_file: self.emit_observatory_command(w_node.observatory_file)

            if step["timer_node"]:
                self.wait_state = "WAITING_TIMER"
                self.timer_start = time.time()
                self.timer_duration = step["timer_node"].duration
                self.log_message(f"Step {self.pc + 1}: Waiting for timer ({self.timer_duration}s)...")
            else:
                self.wait_state = "WAITING_CONFIRMATION"

        elif self.wait_state == "WAITING_TIMER":
            if time.time() - self.timer_start >= self.timer_duration:
                self.wait_state = "WAITING_CONFIRMATION"

        elif self.wait_state == "WAITING_CONFIRMATION":
            for asset in step["assets"]:
                if getattr(asset, 'is_blinking', False): asset.setVisible(self.blink_state)

            ready_to_advance = True
            stall_reasons = []

            for asset in step["assets"]:
                if isinstance(asset, InteractiveMediaItem) and asset.media_type == "Video" and asset.time_hold:
                    if asset.media_player and asset.media_player.mediaStatus() != QMediaPlayer.MediaStatus.EndOfMedia:
                        ready_to_advance = False
                        stall_reasons.append(
                            f"Time Hold: waiting for video '{os.path.basename(asset.filepath)}' to finish playing")
                if isinstance(asset, InteractiveLLMTextItem) and asset.hold_for_response:
                    if not self.check_llm_response_freshness(asset):
                        ready_to_advance = False
                        waited = time.time() - asset.trigger_sent_time
                        stall_reasons.append(
                            f"Hold for Response: waiting on LLM Call '{asset.tool_name}' ({waited:.0f}s elapsed, "
                            f"60s failsafe)")

            for asset in step["assets"]:
                if hasattr(asset, 'trigger'):
                    trig = asset.trigger
                    if trig.wait_type == "Vision Variable":
                        var_name = trig.vision_target
                        if var_name and var_name != "None":
                            current_state = self.vision_tool_states.get(var_name, False)
                            prev_state = self.engine_prev_vision_states.get(var_name, False)
                            if not self.vision_condition_met(trig.condition, current_state, prev_state):
                                ready_to_advance = False
                                stall_reasons.append(
                                    f"Asset vision target '{var_name}' condition '{trig.condition}' not met "
                                    f"(current={current_state}, previous={prev_state})")
                    elif trig.wait_type == "Line":
                        pass  # No gating condition: executes as soon as the step activates.

            for wait_node in step["wait_nodes"]:
                var_name = wait_node.vision_target
                if var_name and var_name != "None":
                    current_state = self.vision_tool_states.get(var_name, False)
                    prev_state = self.engine_prev_vision_states.get(var_name, False)
                    if not self.vision_condition_met(wait_node.condition, current_state, prev_state):
                        ready_to_advance = False
                        stall_reasons.append(
                            f"Wait for Vision '{var_name}' condition '{wait_node.condition}' not met "
                            f"(current={current_state}, previous={prev_state})")

            # User Wait is an ADDITIONAL gate on top of whatever else this
            # step requires, not a replacement for it: the vision/timer/LLM
            # conditions above must genuinely be satisfied first, and only
            # then does it wait for an explicit Back/Forward click before
            # actually advancing. Recorded separately so the buttons
            # (sync_user_wait_buttons) and the click handler
            # (_handle_user_wait_button) can tell "not ready yet" apart from
            # "ready, just waiting on you".
            self.user_wait_conditions_met = ready_to_advance
            if step["user_wait_node"] and ready_to_advance:
                ready_to_advance = False
                stall_reasons.append("User Wait: conditions met - waiting for Back/Forward button click")

            if not ready_to_advance and stall_reasons and time.time() - self.last_stall_log >= 3.0:
                self.last_stall_log = time.time()
                self.log_message(
                    f"<span style='color:#f39c12;'>Step {self.pc + 1} stalled: {'; '.join(stall_reasons)}</span>")

            if ready_to_advance:
                if step["flow_node"]:
                    action = step["flow_node"].action
                    if action == "End Program":
                        self.log_message(f"Step {self.pc + 1} Complete. End Program Reached.")
                        self.enter_design_mode()
                        return
                    elif action == "Loop Continuously":
                        self.log_message(f"Step {self.pc + 1} Complete. Flow Control: Looping to Step 1.")
                        self.pc = 0
                        self.wait_state = "IDLE"
                        return
                    elif action == "Jump to Step Line":
                        target_line = step["flow_node"].target_step
                        target_pc = self.resolve_line_to_step_block_index(target_line)
                        self.log_message(
                            f"Step {self.pc + 1} Complete. Jumping to Line {target_line} (Step Block {target_pc + 1}).")
                        self.pc = target_pc
                        self.wait_state = "IDLE"
                        return

                self.log_message(f"Step {self.pc + 1} Complete. Advancing...")
                self.pc += 1
                self.wait_state = "IDLE"

        # Placed after the state-transition block above (not before it) so
        # that on the very tick an asset first becomes visible, its
        # animation applies immediately - updating before setVisible(True)
        # ran left a one-tick flash at full opacity/resting position before
        # the animation actually kicked in.
        for asset in step["assets"]:
            if hasattr(asset, 'update_animation'):
                asset.update_animation()

        # Also placed after the WAITING_CONFIRMATION block (not before) for
        # the same reason: user_wait_conditions_met is only updated in that
        # block, so syncing the buttons before it ran meant they always
        # reflected the PREVIOUS tick's readiness, one tick stale.
        self.sync_user_wait_buttons()
        self.highlight_current_run_step()

        self.engine_prev_vision_states = self.vision_tool_states.copy()

    def new_project(self):
        """File -> New. Confirms first, because this discards unsaved work.

        new_file() itself stays unprompted: load_project() calls it
        internally when it meets an outdated project format, and a second
        dialog in the middle of a failed load would just be noise.
        """
        confirm = QMessageBox.question(
            self, "New Project",
            "Clear the sequence and every canvas and start a new Studio project?"
            "\n\nAny unsaved work will be lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        # Leave Run Mode before emptying the tree: the sequence engine holds
        # references to tree items, and tearing them out from under a running
        # sequence leaves it pointing at deleted objects. Both calls are
        # idempotent, so this is safe even when already in Design Mode.
        self.stop_sequence()
        self.enter_design_mode()
        self.new_file()
        self.log_message("New project started.")

    def new_file(self):
        self.active_item = None
        for c in self.canvases:
            c.clear_canvas()
        self.step_tree.clear()
        self.add_new_step_node()

    def add_new_step_node(self):
        idx = self.step_tree.topLevelItemCount() + 1
        top_item = QTreeWidgetItem(self.step_tree, ["", f"Step Block [{idx}]", "", ""])
        top_item.setBackground(1, QColor("#1e508c"))
        top_item.setForeground(1, QColor("white"))
        font = QFont()
        font.setBold(True)
        top_item.setFont(1, font)
        top_item.setExpanded(True)
        self.step_tree.setCurrentItem(top_item)
        self.renumber_sequence()

    def browse_media(self, media_type):
        filepath = self.prompt_open_file(f"Insert {media_type}", "All Files (*)")
        if filepath: self.insert_asset(InteractiveMediaItem(filepath, media_type), f"Media [{media_type}]")

    def insert_shape_dialog(self):
        options = ["Square", "Circle", "Triangle", "Custom SVG (browse template library)..."]
        choice, ok = QInputDialog.getItem(self, "Insert Shape", "Choose a shape template:", options, 0, False)
        if not ok:
            return
        if choice.startswith("Custom SVG"):
            self.browse_custom_svg()
        else:
            self.insert_asset(InteractiveShapeItem(choice), f"◆ Shape [{choice}]")

    def browse_custom_svg(self):
        shape_lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shape_library")
        filepath = self.prompt_open_file("Insert Custom SVG", "SVG Files (*.svg)", start_dir=shape_lib_dir)
        if filepath:
            self.insert_asset(InteractiveShapeItem("Custom SVG", filepath), "◆ Shape [Custom SVG]")

    def insert_html_dialog(self):
        # Matches every other "Insert X" button (Shape, Text, ...): appears
        # immediately at the default spot with a placeholder page - no
        # upfront dialog - and the URL/local file is set afterward from the
        # properties panel, same place Size/Rotation/etc. already live.
        try:
            self.insert_asset(InteractiveHTMLItem(), "🌐 HTML Window")
        except Exception as e:
            self.log_message(
                f"<span style='color:red;'>Could not create HTML window: {e}. Is PyQt6-WebEngine "
                f"installed for the Python interpreter actually running this app?</span>")

    def insert_3d_model(self):
        # Inserted empty, file chosen afterward from properties - same as
        # Shape/HTML, and it keeps the (potentially very slow) STEP load out
        # of the insert click.
        self.insert_asset(Interactive3DModelItem(), "🧊 3D Model")

    def insert_capture_image(self):
        existing = sum(1 for i in self.all_canvas_items()
                       if isinstance(i, InteractiveCaptureItem))
        item = InteractiveCaptureItem(item_name=f"Capture {existing + 1}",
                                      observatory_file=self.last_commanded_file or "")
        self.insert_asset(item, "📷 Capture Image")

    def insert_asset(self, item_obj, name="Asset"):
        parent = self.get_active_step_parent()
        item_obj.setPos(200, 200)
        # Anchor the coordinate reference at the moment of insertion, while
        # the position is known. Leaving it to be captured lazily on first
        # read meant that moving an item before ever opening its properties
        # anchored the reference to wherever it had been dragged, so the
        # readout showed 0,0 no matter where it sat.
        if hasattr(item_obj, 'set_home_position'):
            item_obj.set_home_position()
        self.studio_scene.addItem(item_obj)
        ti = QTreeWidgetItem(parent, ["", name, "Active in Step", ""])
        ti.setData(1, Qt.ItemDataRole.UserRole, item_obj)
        self.renumber_sequence()

    def insert_llm_call(self):
        self.insert_asset(InteractiveLLMTextItem(), "🧠 LLM Call")

    def insert_clear_canvas(self):
        parent = self.get_active_step_parent()
        ti = QTreeWidgetItem(parent, ["", "🧹 Clear Canvas", "Executes Immediately", ""])
        ti.setData(1, Qt.ItemDataRole.UserRole, ClearCanvasData())
        self.renumber_sequence()

    def insert_timer(self):
        parent = self.get_active_step_parent()
        data = TimerData()
        ti = QTreeWidgetItem(parent, ["", "⏳ Timer", "Wait 5.0 sec", ""])
        ti.setData(1, Qt.ItemDataRole.UserRole, data)
        ti.setForeground(1, QColor("#d35400"))
        self.renumber_sequence()

    def insert_observatory_node(self):
        parent = self.get_active_step_parent()
        data = ObservatoryNodeData()
        ti = QTreeWidgetItem(parent, ["", "👁️ Load Observatory Project", "No File", ""])
        ti.setData(1, Qt.ItemDataRole.UserRole, data)
        ti.setForeground(1, QColor("#2980b9"))
        self.renumber_sequence()

    def insert_vision_wait(self):
        parent = self.get_active_step_parent()
        data = VisionWaitData()
        ti = QTreeWidgetItem(parent, ["", "⏳ Wait for Vision", "Target: None", ""])
        ti.setData(1, Qt.ItemDataRole.UserRole, data)
        ti.setForeground(1, QColor("#8e44ad"))
        self.renumber_sequence()

    def insert_flow_control(self):
        parent = self.get_active_step_parent()
        data = FlowControlData()
        ti = QTreeWidgetItem(parent, ["", "🛑 Flow Control", "Action: End Program", ""])
        ti.setData(1, Qt.ItemDataRole.UserRole, data)
        ti.setForeground(1, QColor("#e74c3c"))
        self.renumber_sequence()

    def insert_user_wait(self):
        parent = self.get_active_step_parent()
        data = UserWaitData()
        ti = QTreeWidgetItem(parent, ["", "👆 User Wait", "Fwd: Next Step | Back: Previous Step", ""])
        ti.setData(1, Qt.ItemDataRole.UserRole, data)
        ti.setForeground(1, QColor("#16a085"))
        self.renumber_sequence()

    def all_canvas_items(self):
        """Every asset across every output canvas. Most engine/lifecycle
        operations (hide-all, stop videos, cleanup) need to reach assets on
        secondary canvases too, not just canvas 0's scene."""
        items = []
        for c in self.canvases:
            items.extend(c.scene.items())
        return items

    def refresh_preview_canvas_selector(self):
        """Keep the Run Mode preview picker in step with the canvas count."""
        try:
            multi = len(self.canvases) > 1
            self.lbl_preview_canvas.setVisible(multi)
            self.cb_preview_canvas.setVisible(multi)

            previous = self.cb_preview_canvas.currentData()
            self.cb_preview_canvas.blockSignals(True)
            self.cb_preview_canvas.clear()
            for i in range(len(self.canvases)):
                self.cb_preview_canvas.addItem(f"Canvas {i + 1}", i)
            restore = self.cb_preview_canvas.findData(previous if previous is not None else 0)
            self.cb_preview_canvas.setCurrentIndex(restore if restore >= 0 else 0)
            self.cb_preview_canvas.blockSignals(False)

            # A canvas that just disappeared would leave the view showing a
            # dead scene, so re-point it at whatever is now selected.
            self.set_preview_canvas(self.cb_preview_canvas.currentIndex())
        except Exception as e:
            self.log_message(f"<span style='color:red;'>Preview selector refresh failed: {e}</span>")

    def set_preview_canvas(self, combo_index):
        try:
            idx = self.cb_preview_canvas.itemData(combo_index)
            if idx is None:
                idx = 0
            idx = max(0, min(len(self.canvases) - 1, int(idx)))
            self.run_preview_view.setScene(self.canvases[idx].scene)
            self.run_preview_view.fitInView(
                self.run_preview_view.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        except Exception as e:
            self.log_message(f"<span style='color:red;'>Preview canvas switch failed: {e}</span>")

    def set_canvas_count(self, count):
        try:
            count = max(1, min(5, int(count)))
            current = len(self.canvases)
            if count == current:
                return

            if count > current:
                for idx in range(current, count):
                    new_canvas = ProjectorCanvas()
                    new_canvas.setWindowTitle(f"Output Canvas {idx + 1}")
                    # Offset each extra window so they don't stack exactly on
                    # top of each other on a single-monitor setup.
                    new_canvas.setGeometry(50 + idx * 40, 50 + idx * 40, 800, 600)
                    new_canvas.show()
                    self.canvases.append(new_canvas)
                self.log_message(f"Output canvases: {count}.")
            else:
                # Reassign any assets living on a canvas that's going away
                # back to canvas 0, so they're never orphaned into a scene
                # with no window.
                for idx in range(count, current):
                    doomed = self.canvases[idx]
                    for item in list(doomed.scene.items()):
                        if item.parentItem() is None and hasattr(item, 'output_canvas'):
                            doomed.scene.removeItem(item)
                            item.output_canvas = 0
                            self.studio_scene.addItem(item)
                    doomed.close()
                del self.canvases[count:]
                self.log_message(f"Output canvases: {count}. Assets from removed canvases moved to Canvas 1.")
            self.refresh_preview_canvas_selector()
        except Exception as e:
            self.log_message(f"<span style='color:red;'>Canvas count change failed: {e}</span>")

    def assign_asset_to_canvas(self, item, canvas_index):
        try:
            canvas_index = max(0, min(len(self.canvases) - 1, int(canvas_index)))
            if getattr(item, 'output_canvas', 0) == canvas_index and item.scene() is self.canvases[canvas_index].scene:
                return
            was_visible = item.isVisible()
            if item.scene() is not None:
                item.scene().removeItem(item)
            item.output_canvas = canvas_index
            self.canvases[canvas_index].scene.addItem(item)
            item.setVisible(was_visible)
        except Exception as e:
            self.log_message(f"<span style='color:red;'>Could not move asset to canvas {canvas_index + 1}: {e}</span>")

    def build_output_canvas_selector(self, item):
        cb = QComboBox()
        for i in range(len(self.canvases)):
            cb.addItem(f"Canvas {i + 1}", i)
        cb.setCurrentIndex(min(getattr(item, 'output_canvas', 0), len(self.canvases) - 1))
        cb.currentIndexChanged.connect(lambda idx: self.assign_asset_to_canvas(item, idx))
        self.prop_form.addRow("Output Canvas:", cb)

    def get_active_step_parent(self):
        sel = self.step_tree.selectedItems()
        if not sel: return self.step_tree.topLevelItem(max(0, self.step_tree.topLevelItemCount() - 1))
        return sel[0].parent() if sel[0].parent() else sel[0]

    def delete_selected_tree_node(self):
        sel = self.step_tree.selectedItems()
        if not sel: return
        item = sel[0]
        asset = item.data(1, Qt.ItemDataRole.UserRole)
        if isinstance(asset, QGraphicsItem) and asset.scene() is not None:
            asset.scene().removeItem(asset)

        if item.parent():
            item.parent().removeChild(item)
        else:
            self.step_tree.invisibleRootItem().removeChild(item)

        self.renumber_sequence()

    def renumber_sequence(self, *args):
        line_num = 1
        for i in range(self.step_tree.topLevelItemCount()):
            top = self.step_tree.topLevelItem(i)
            top.setText(0, str(line_num))
            line_num += 1

            for j in range(top.childCount()):
                child = top.child(j)
                child.setText(0, str(line_num))
                line_num += 1

    def resolve_line_to_step_block_index(self, line_number):
        # "Jump to Step Line" targets the visible "#" column line number (which
        # counts every row, headers and children alike), not the Step Block's
        # own [N] ordinal - so a jump to a child line lands on whichever Step
        # Block actually contains that line.
        line_num = 1
        for i in range(self.step_tree.topLevelItemCount()):
            top = self.step_tree.topLevelItem(i)
            if line_num == line_number:
                return i
            line_num += 1

            for j in range(top.childCount()):
                if line_num == line_number:
                    return i
                line_num += 1

        return max(0, self.step_tree.topLevelItemCount() - 1)

    def _handle_user_wait_button(self, direction):
        if not self.is_running or self.pc >= len(self.execution_sequence):
            return
        step = self.execution_sequence[self.pc]
        node = step.get("user_wait_node")
        if not node:
            self.log_message("User Wait: no button action configured for the current step - click ignored.")
            return
        if not getattr(self, 'user_wait_conditions_met', False):
            self.log_message(
                "User Wait: this step's other conditions (vision/timer/LLM) aren't met yet - click ignored.")
            return

        if direction == "forward":
            action, target = node.forward_action, node.forward_target_step
            label = "Forward"
        else:
            action, target = node.backward_action, node.backward_target_step
            label = "Back"

        if action == "Next Step":
            self.pc += 1
        elif action == "Previous Step":
            self.pc = max(0, self.pc - 1)
        elif action == "Jump to Step Line":
            self.pc = self.resolve_line_to_step_block_index(target)

        self.wait_state = "IDLE"
        self.log_message(f"User Wait: '{label}' button clicked -> {action}.")

    def sync_user_wait_buttons(self):
        # Only enabled once this step's OTHER conditions (vision/timer/LLM)
        # are actually satisfied - before that, clicking would have nothing
        # to do, since User Wait adds to those conditions rather than
        # replacing them.
        enabled = (self.is_running and self.pc < len(self.execution_sequence)
                   and self.execution_sequence[self.pc]["user_wait_node"] is not None
                   and getattr(self, 'user_wait_conditions_met', False))
        self.btn_user_wait_back.setEnabled(enabled)
        self.btn_user_wait_forward.setEnabled(enabled)

    def build_run_sequence_mirror(self):
        # A plain, read-only clone of the step tree's current text/columns -
        # not the live, editable step_tree itself, so nothing here can
        # accidentally be dragged, edited, or deleted mid-run.
        self.run_sequence_view.clear()
        for i in range(self.step_tree.topLevelItemCount()):
            top = self.step_tree.topLevelItem(i)
            top_copy = QTreeWidgetItem(self.run_sequence_view,
                                       [top.text(c) for c in range(top.columnCount())])
            for j in range(top.childCount()):
                child = top.child(j)
                QTreeWidgetItem(top_copy, [child.text(c) for c in range(child.columnCount())])
            top_copy.setExpanded(True)

    def highlight_current_run_step(self):
        # setBackground() turned out to be silently swallowed by this
        # widget's own QSS (QTreeWidget::item has stylesheet rules defined,
        # so Qt's stylesheet engine takes over item background painting
        # entirely and ignores the palette-based background role) - the row
        # stayed dark regardless of what color was set here, while the
        # black foreground DID apply, making the text invisible. Foreground
        # color isn't swallowed the same way, so a bright, high-contrast
        # text color (no background change at all) is what actually renders.
        highlight_fg = QColor("#ffeb3b")  # vivid yellow
        normal_fg = QColor("#a9b7c6")  # matches QTreeWidget's own default item color
        bold_font = QFont()
        bold_font.setBold(True)
        normal_font = QFont()

        for i in range(self.run_sequence_view.topLevelItemCount()):
            top = self.run_sequence_view.topLevelItem(i)
            active = i == self.pc
            fg = highlight_fg if active else normal_fg
            font = bold_font if active else normal_font
            for col in range(top.columnCount()):
                top.setForeground(col, fg)
                top.setFont(col, font)
            for j in range(top.childCount()):
                child = top.child(j)
                for col in range(child.columnCount()):
                    child.setForeground(col, fg)
                    child.setFont(col, font)
        if 0 <= self.pc < self.run_sequence_view.topLevelItemCount():
            self.run_sequence_view.scrollToItem(self.run_sequence_view.topLevelItem(self.pc))

    def handle_tree_selection(self):
        sel = self.step_tree.selectedItems()
        if not sel: return
        asset = sel[0].data(1, Qt.ItemDataRole.UserRole)

        self.prop_group.setVisible(False)
        self.node_prop_group.setVisible(False)

        if isinstance(asset, QGraphicsItem):
            self.studio_scene.clearSelection()
            asset.setSelected(True)
            self.prop_group.setVisible(True)
            self.active_item = asset
            self.build_asset_properties(asset)
        elif asset:
            self.node_prop_group.setVisible(True)
            self.active_item = asset
            self.build_node_properties(asset)

    def handle_tree_double_click(self, tree_item, column):
        if not self.in_design_mode: return

        if tree_item.parent() is None:
            assets = [tree_item.child(j).data(1, Qt.ItemDataRole.UserRole) for j in range(tree_item.childCount())]
        else:
            assets = [tree_item.data(1, Qt.ItemDataRole.UserRole)]

        self.hide_all_canvas_items()

        for asset in assets:
            if isinstance(asset, QGraphicsItem):
                asset.setVisible(True)
                if isinstance(asset, InteractiveLLMTextItem):
                    self.emit_llm_trigger_command(asset, force=True)
                if isinstance(asset, InteractiveMediaItem) and asset.media_type == "Video":
                    asset.play_from_start()
            elif isinstance(asset, ObservatoryNodeData) and asset.filepath:
                self.emit_observatory_command(asset.filepath, force=True)

        self.log_message(f"Preview: Executed '{tree_item.text(1)}' on Output Canvas.")

    def update_tree_labels(self):
        if isinstance(self.active_item, FlowControlData):
            for item in self.step_tree.selectedItems():
                item.setText(2, f"Action: {self.active_item.action}")
                item.setText(3,
                             f"Target Line: {self.active_item.target_step}" if self.active_item.action == "Jump to Step Line" else "")
        elif isinstance(self.active_item, TimerData):
            for item in self.step_tree.selectedItems(): item.setText(2, f"Wait {self.active_item.duration:.1f} sec")
        elif isinstance(self.active_item, ObservatoryNodeData):
            for item in self.step_tree.selectedItems(): item.setText(2, os.path.basename(
                self.active_item.filepath) if self.active_item.filepath else "No File")
        elif isinstance(self.active_item, ClearCanvasData):
            target = self.active_item.output_canvas
            label = "All Canvases" if target < 0 else f"Canvas {target + 1}"
            for item in self.step_tree.selectedItems():
                item.setText(2, label)
        elif isinstance(self.active_item, VisionWaitData):
            for item in self.step_tree.selectedItems():
                item.setText(2, f"Target: {self.active_item.vision_target}")
                item.setText(3, self.active_item.condition)
        elif isinstance(self.active_item, UserWaitData):
            fwd = self.active_item.forward_action
            back = self.active_item.backward_action
            fwd_txt = f"{fwd} ({self.active_item.forward_target_step})" if fwd == "Jump to Step Line" else fwd
            back_txt = f"{back} ({self.active_item.backward_target_step})" if back == "Jump to Step Line" else back
            for item in self.step_tree.selectedItems():
                item.setText(2, f"Fwd: {fwd_txt} | Back: {back_txt}")

    def handle_canvas_selection(self):
        selected = self.studio_scene.selectedItems()
        if not selected: return
        item = selected[0]

        for i in range(self.step_tree.topLevelItemCount()):
            top = self.step_tree.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                if child.data(1, Qt.ItemDataRole.UserRole) == item:
                    self.step_tree.blockSignals(True)
                    child.setSelected(True)
                    self.step_tree.scrollToItem(child)
                    self.step_tree.blockSignals(False)
                    self.prop_group.setVisible(True)
                    self.node_prop_group.setVisible(False)
                    self.active_item = item
                    self.build_asset_properties(item)
                    return

    def log_message(self, msg):
        self.console.append(f"<span style='color:#00FF00;'>[{time.strftime('%H:%M:%S')}] {msg}</span>")

    def safe_slot(self, fn, *args, **kwargs):
        # QTimer.singleShot callbacks fire through the same Qt signal-dispatch
        # path as recurring timers: an uncaught exception there gets escalated
        # to a hard process abort by this PyQt6 build instead of a catchable
        # traceback. Every deferred callback must go through this.
        def wrapper():
            try:
                fn(*args, **kwargs)
            except Exception as e:
                self.log_message(f"<span style='color:red;'>Deferred callback error: {e}</span>")
        return wrapper


def main():
    # Required before QApplication is created if anything in this process
    # ever constructs a QWebEngineView (InteractiveHTMLItem does, lazily) -
    # setting it here up front means the HTML item's own lazy import doesn't
    # have to race against whether a QApplication already exists.
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_THEME)
    canvas = ProjectorCanvas()
    authoring = AuthoringInterface(canvas)

    canvas.show()
    authoring.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()