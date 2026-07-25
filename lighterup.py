import sys
import os
import time
import json
import subprocess

from PyQt6.QtWidgets import *
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QColor, QBrush, QFont, QPen, QPixmap

# Custom components imports
from vision_tools import SearchROI, PatternROI, PerspectivePlaneROI
from ui_components import DARK_THEME, LLMWorker, VisionCanvasView, ThresholdScoreBar

# Professional Dark Theme Stylesheet
DARK_THEME = """
QWidget { background-color: #2b2b2b; color: #a9b7c6; font-family: 'Segoe UI', Arial; }
QPushButton { background-color: #4C5052; border: 1px solid #5C5C42; padding: 5px 15px; border-radius: 3px; color: #ffffff; }
QPushButton:hover { background-color: #5C6062; }
QPushButton:pressed { background-color: #3C3F41; }
QPushButton:disabled { background-color: #1e1e1e; color: #555555; border: 1px solid #333333; }
QListWidget { background-color: #313335; border: 1px solid #1e1e1e; }
QTreeWidget { background-color: #1e1e1e; border: 1px solid #555; color: #a9b7c6; }
QTreeWidget::item { padding: 4px; border-bottom: 1px solid #2b2b2b; }
QTreeWidget::item:selected { background-color: #2f65ca; color: white; }
QHeaderView::section { background-color: #3C3F41; padding: 4px; border: 1px solid #1e1e1e; font-weight: bold; }
QTextBrowser { background-color: #1e1e1e; color: #a9b7c6; border: 1px solid #555555; font-family: 'Consolas', monospace; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QTextEdit { background-color: #3C3F41; border: 1px solid #1e1e1e; padding: 3px; color: #ffffff; }
QMenuBar { background-color: #3C3F41; border-bottom: 1px solid #1e1e1e; }
QGroupBox { border: 1px solid #555555; margin-top: 10px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
"""


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
    pass


class ObservatoryNodeData:
    def __init__(self, filepath=""):
        self.filepath = filepath


class VisionWaitData:
    def __init__(self):
        self.observatory_file = ""
        self.vision_target = "None"
        self.condition = "Is True"


class TriggerSettings:
    def __init__(self):
        self.wait_type = "Manual Confirmation"
        self.observatory_file = ""
        self.vision_target = "None"
        self.condition = "Is True"


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
        super().dropEvent(event)
        # Defer the renumbering by 10ms to let PyQt finish the drop animation natively
        QTimer.singleShot(10, self.parent_interface.renumber_sequence)


# ---------------------------------------------------------
# 2. CUSTOM PROJECTOR CANVAS ASSETS
# ---------------------------------------------------------
class InteractiveTextItem(QGraphicsTextItem):
    def __init__(self, text="New Text"):
        super().__init__(text)
        self.trigger = TriggerSettings()
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.current_font_size = 48
        self.setFont(QFont("Arial", self.current_font_size))
        self.setDefaultTextColor(QColor("white"))


class InteractiveMediaItem(QGraphicsRectItem):
    def __init__(self, filepath="Placeholder", media_type="Image"):
        super().__init__(0, 0, 200, 150)
        self.trigger = TriggerSettings()
        self.filepath = filepath
        self.media_type = media_type
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)

        if media_type == "Image" and os.path.exists(filepath):
            pixmap = QPixmap(filepath).scaled(400, 400, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation)
            self.pixmap_item = QGraphicsPixmapItem(pixmap, self)
            self.setRect(self.pixmap_item.boundingRect())
            self.setPen(QPen(Qt.PenStyle.NoPen))
        else:
            color_hex, icon = "#3498db", "🎬" if media_type == "Video" else "🎨"
            self.setPen(QPen(QColor(color_hex), 2))
            self.setBrush(QBrush(QColor(40, 40, 40, 200)))
            self.text_tag = QGraphicsTextItem(f"{icon} {os.path.basename(filepath)}", self)
            self.text_tag.setDefaultTextColor(QColor("white"))
            self.text_tag.setPos(5, 5)


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


class AuthoringInterface(QMainWindow):
    def __init__(self, canvas):
        super().__init__()
        self.setWindowTitle("LightGuide Studio - Authoring Environment")
        self.setGeometry(200, 200, 1500, 850)

        self.canvas, self.studio_scene = canvas, canvas.scene
        self.active_item = None

        self.is_running = False
        self.execution_sequence = []
        self.pc = 0
        self.wait_state = "IDLE"
        self.manual_pass_flag = False
        self.timer_start = 0
        self.timer_duration = 0

        self.last_commanded_file = None
        self.vision_tool_states = {"None": True}
        self.engine_prev_vision_states = {"None": True}

        self.setup_ui()

        self.step_tree.itemSelectionChanged.connect(self.handle_tree_selection)
        self.studio_scene.selectionChanged.connect(self.handle_canvas_selection)

        self.blink_state = True
        self.master_clock = QTimer()
        self.master_clock.timeout.connect(self.process_execution_engine)
        self.master_clock.start(100)

        self.ipc_timer = QTimer()
        self.ipc_timer.timeout.connect(self.read_live_vision_state)
        self.ipc_timer.start(50)

        self.enter_design_mode()

        # Auto-launch Observatory on startup
        QTimer.singleShot(500, self.launch_observatory)

    def emit_observatory_command(self, filepath=None):
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
                    elif isinstance(asset, ObservatoryNodeData) and asset.filepath:
                        if os.path.exists(asset.filepath):
                            filepath = asset.filepath
                            break
                if filepath: break

        if filepath and filepath != self.last_commanded_file and os.path.exists(filepath):
            try:
                command = {"action": "load_and_start", "filepath": filepath, "timestamp": time.time()}
                with open("studio_command.json", "w") as f:
                    json.dump(command, f)
                self.last_commanded_file = filepath
                self.log_message(f"Commanded Observatory to load: {os.path.basename(filepath)}")
            except Exception as e:
                self.log_message(f"IPC Error: {e}")

    def parse_observatory_file_silent(self, filepath):
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            return ["None"] + [t["name"] for t in data.get("tools", []) if "name" in t]
        except Exception:
            return ["None"]

    def save_project(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Project", "", "Project Files (*.json)")
        if not path: return

        scene_data = []
        for item in self.studio_scene.items():
            if hasattr(item, 'trigger'):
                item_info = {
                    "type": type(item).__name__,
                    "pos": [item.pos().x(), item.pos().y()],
                    "text": item.toPlainText() if hasattr(item, 'toPlainText') else "",
                    "filepath": getattr(item, 'filepath', ""),
                    "media_type": getattr(item, 'media_type', ""),
                    "font_size": getattr(item, 'current_font_size', 48),
                    "rotation": item.rotation(),
                    "trigger": vars(item.trigger)
                }
                scene_data.append(item_info)

        with open(path, 'w') as f:
            json.dump(scene_data, f, indent=4)
        self.log_message(f"Project saved to {os.path.basename(path)}")

    def load_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Project", "", "Project Files (*.json)")
        if not path: return
        self.studio_scene.clear()

        try:
            with open(path, 'r') as f:
                scene_data = json.load(f)
                for d in scene_data:
                    if d['type'] == 'InteractiveTextItem':
                        new_item = InteractiveTextItem(d.get('text', "Text"))
                        new_item.current_font_size = d.get('font_size', 48)
                        new_item.setFont(QFont("Arial", new_item.current_font_size))
                    elif d['type'] == 'InteractiveMediaItem':
                        new_item = InteractiveMediaItem(d.get('filepath', ""), d.get('media_type', "Image"))

                    new_item.trigger.__dict__.update(d.get('trigger', {}))
                    new_item.setPos(d['pos'][0], d['pos'][1])
                    new_item.setRotation(d.get('rotation', 0))
                    self.studio_scene.addItem(new_item)
            self.log_message("Project loaded.")
        except Exception as e:
            self.log_message(f"<span style='color:red;'>Failed to load project: {e}</span>")

    def setup_ui(self):
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")
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
        self.btn_manual_confirm = QPushButton("✅ CONFIRM STEP")
        self.btn_manual_confirm.setStyleSheet("background-color: #f39c12; font-weight: bold; color: black;")
        self.btn_manual_confirm.clicked.connect(self.trigger_manual_pass)

        for btn in [self.btn_restart, self.btn_play, self.btn_stop, self.btn_manual_confirm]:
            btn.setVisible(False)

        btn_launch_obs = QPushButton("👁️ Launch Observatory")
        btn_launch_obs.setStyleSheet("background-color: #3498db; font-weight: bold; color: white;")
        btn_launch_obs.clicked.connect(self.launch_observatory)

        toolbar.addWidget(self.btn_design_mode)
        toolbar.addWidget(self.btn_run_mode)
        toolbar.addWidget(self.btn_restart)
        toolbar.addWidget(self.btn_play)
        toolbar.addWidget(self.btn_stop)
        toolbar.addWidget(self.btn_manual_confirm)
        toolbar.addStretch()
        toolbar.addWidget(btn_launch_obs)
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

        for b in [btn_step, btn_txt, btn_img, btn_vid, btn_clear, btn_timer, btn_obs, btn_wait, btn_flow]:
            l_lay.addWidget(b)

        btn_del = QPushButton("🗑️ Delete Selected Line")
        btn_del.clicked.connect(self.delete_selected_tree_node)
        l_lay.addStretch()
        l_lay.addWidget(btn_del)
        h_splitter.addWidget(left)

        center = QWidget()
        c_lay = QVBoxLayout(center)
        self.step_tree = SequenceTreeWidget(self)
        c_lay.addWidget(self.step_tree)
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

    def launch_observatory(self):
        try:
            subprocess.Popen([sys.executable, "main.py"])
            self.log_message("Launching Observatory Engine...")
        except Exception as e:
            self.log_message(f"<span style='color:red;'>Failed to launch Observatory: {e}</span>")

    def read_live_vision_state(self):
        try:
            if os.path.exists("vision_state.json"):
                with open("vision_state.json", "r") as f:
                    self.vision_tool_states = json.load(f)
        except Exception:
            pass

    def clear_layout(self, layout):
        if isinstance(layout, QFormLayout):
            while layout.rowCount() > 0: layout.removeRow(0)
        else:
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
                elif item.layout():
                    self.clear_layout(item.layout()); item.layout().deleteLater()

    def build_asset_properties(self, item):
        try:
            self.clear_layout(self.prop_form)
            self.prop_form.addRow(QLabel("<b>Asset Properties</b>"))
            if isinstance(item, InteractiveTextItem):
                txt = QLineEdit(item.toPlainText());
                txt.editingFinished.connect(lambda: item.setPlainText(txt.text()));
                self.prop_form.addRow("Text:", txt)
                spin_size = QSpinBox();
                spin_size.setRange(8, 200);
                spin_size.setValue(item.current_font_size);
                spin_size.valueChanged.connect(lambda v: self.apply_font_size(item, v));
                self.prop_form.addRow("Font Size:", spin_size)
                spin_rot = QSpinBox();
                spin_rot.setRange(0, 359);
                spin_rot.setValue(int(item.rotation()));
                spin_rot.valueChanged.connect(lambda v: item.setRotation(v));
                self.prop_form.addRow("Rotation:", spin_rot)
            elif isinstance(item, InteractiveMediaItem):
                self.prop_form.addRow("File:", QLabel(os.path.basename(item.filepath)))
                spin_rot = QSpinBox();
                spin_rot.setRange(0, 359);
                spin_rot.setValue(int(item.rotation()));
                spin_rot.valueChanged.connect(lambda v: item.setRotation(v));
                self.prop_form.addRow("Rotation:", spin_rot)
            if hasattr(item, 'trigger'):
                self.prop_form.addRow(QLabel("<hr><b>Confirmation Settings</b>"))
                cb_type = QComboBox();
                cb_type.addItems(["Manual Confirmation", "Vision Variable"]);
                cb_type.setCurrentText(item.trigger.wait_type);
                cb_type.currentTextChanged.connect(lambda v: setattr(item.trigger, 'wait_type', v));
                self.prop_form.addRow("Trigger Type:", cb_type)
        except Exception as e:
            print(f"UI BUILDER ERROR: {e}")

    def build_node_properties(self, asset):
        try:
            self.clear_layout(self.node_prop_form)
            if isinstance(asset, FlowControlData):
                self.node_prop_form.addRow(QLabel("<b>Flow Control Settings</b>"))
                cb_action = QComboBox();
                cb_action.addItems(["End Program", "Loop Continuously", "Jump to Step Line"]);
                cb_action.setCurrentText(asset.action);
                cb_action.currentTextChanged.connect(lambda v: setattr(asset, 'action', v));
                self.node_prop_form.addRow("Action:", cb_action)
                spin_target = QSpinBox();
                spin_target.setRange(1, 100);
                spin_target.setValue(asset.target_step);
                spin_target.valueChanged.connect(lambda v: setattr(asset, 'target_step', v));
                self.node_prop_form.addRow("Target Step:", spin_target)
            elif isinstance(asset, TimerData):
                self.node_prop_form.addRow(QLabel("<b>Timer Settings</b>"))
                spin_dur = QDoubleSpinBox();
                spin_dur.setRange(0.1, 3600.0);
                spin_dur.setValue(asset.duration);
                spin_dur.valueChanged.connect(lambda v: setattr(asset, 'duration', v));
                self.node_prop_form.addRow("Duration (sec):", spin_dur)
            elif isinstance(asset, ObservatoryNodeData):
                self.node_prop_form.addRow(QLabel("<b>Observatory Auto-Load Node</b>"))
                file_lay = QHBoxLayout();
                lbl_file = QLineEdit(os.path.basename(asset.filepath) if asset.filepath else "None");
                lbl_file.setReadOnly(True);
                btn_file = QPushButton("Browse");
                btn_file.clicked.connect(lambda: self.browse_node_file(asset, lbl_file));
                file_lay.addWidget(lbl_file);
                file_lay.addWidget(btn_file);
                self.node_prop_form.addRow("Obs File:", file_lay)
        except Exception as e:
            print(f"NODE UI ERROR: {e}")

    def apply_font_size(self, item, size):
        item.current_font_size = size; item.setFont(QFont("Arial", size))

    def browse_file_for_item(self, item, line_edit):
        filepath, _ = QFileDialog.getOpenFileName(self, "Select Observatory Project", "", "JSON Files (*.json)")
        if filepath: item.trigger.observatory_file = filepath; line_edit.setText(
            os.path.basename(filepath)); self.build_asset_properties(item)

    def browse_node_file(self, asset, line_edit):
        filepath, _ = QFileDialog.getOpenFileName(self, "Select Observatory Project", "", "JSON Files (*.json)")
        if filepath: asset.filepath = filepath; line_edit.setText(os.path.basename(filepath))

    def enter_design_mode(self):
        self.is_running = False
        for btn in [self.btn_restart, self.btn_play, self.btn_stop, self.btn_manual_confirm]: btn.setVisible(False)
        self.btn_design_mode.setStyleSheet("background-color: #2ecc71; font-weight: bold; color: black;");
        self.btn_run_mode.setStyleSheet("background-color: #3C3F41; color: white;")
        for item in self.studio_scene.items(): item.setVisible(True)
        self.log_message("Authoring: Design Mode active.")

    def enter_run_mode(self):
        self.btn_design_mode.setStyleSheet("background-color: #3C3F41; color: white;");
        self.btn_run_mode.setStyleSheet("background-color: #e74c3c; font-weight: bold; color: white;")
        for btn in [self.btn_restart, self.btn_play, self.btn_stop, self.btn_manual_confirm]: btn.setVisible(True)
        for item in self.studio_scene.items(): item.setVisible(False)
        self.execution_sequence = self.build_execution_sequence();
        self.wait_state = "IDLE";
        self.manual_pass_flag = False
        sel = self.step_tree.selectedItems();
        self.pc = max(0,
                      self.step_tree.indexOfTopLevelItem(sel[0].parent() if sel[0].parent() else sel[0])) if sel else 0
        self.is_running = False;
        self.update_playback_ui();
        self.log_message(f"Run Mode ready. Paused at Step {self.pc + 1}.")

    def play_sequence(self):
        self.is_running = True; self.log_message(f"Playing from step {self.pc + 1}..."); self.update_playback_ui()

    def stop_sequence(self):
        self.is_running = False; self.log_message("Execution Paused/Stopped."); self.update_playback_ui()

    def restart_sequence(self):
        self.pc = 0; self.wait_state = "IDLE"; self.is_running = True; self.log_message(
            "Restarting sequence from Step 1..."); self.update_playback_ui()

    def update_playback_ui(self):
        if self.is_running:
            self.btn_play.setStyleSheet(
                "background-color: #2ecc71; color: black; font-weight: bold;"); self.btn_stop.setStyleSheet(
                "background-color: #3C3F41; color: white;")
        else:
            self.btn_play.setStyleSheet("background-color: #3C3F41; color: white;"); self.btn_stop.setStyleSheet(
                "background-color: #e74c3c; color: white; font-weight: bold;")

    def trigger_manual_pass(self):
        if self.is_running and self.wait_state == "WAITING_CONFIRMATION": self.manual_pass_flag = True; self.log_message(
            "System: Manual Pass Confirmed.")

    def build_execution_sequence(self):
        seq = []
        for i in range(self.step_tree.topLevelItemCount()):
            top_node = self.step_tree.topLevelItem(i)
            step_data = {"assets": [], "flow_node": None, "clear_node": None, "timer_node": None, "obs_node": None,
                         "wait_nodes": []}
            for j in range(top_node.childCount()):
                child = top_node.child(j);
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
                elif asset:
                    step_data["assets"].append(asset)
            seq.append(step_data)
        return seq

    def process_execution_engine(self):
        self.blink_state = not self.blink_state
        if not self.is_running: return
        if self.pc >= len(self.execution_sequence): self.enter_design_mode(); self.log_message(
            "Program complete."); return
        step = self.execution_sequence[self.pc]
        if self.wait_state == "IDLE":
            for i in range(self.step_tree.topLevelItemCount()):
                top = self.step_tree.topLevelItem(i);
                active = (i == self.pc)
                top.setBackground(1, QColor("#2ecc71") if active else QColor("#1e508c"))
            if step["clear_node"]:
                for item in self.studio_scene.items(): item.setVisible(False)
            if step["obs_node"] and step["obs_node"].filepath: self.emit_observatory_command(step["obs_node"].filepath)
            for asset in step["assets"]: asset.setVisible(True)
            self.wait_state = "WAITING_CONFIRMATION"
        elif self.wait_state == "WAITING_CONFIRMATION":
            ready_to_advance = True
            for asset in step["assets"]:
                if hasattr(asset,
                           'trigger') and asset.trigger.wait_type == "Manual Confirmation" and not self.manual_pass_flag: ready_to_advance = False
            if ready_to_advance:
                if step["flow_node"]:
                    action = step["flow_node"].action
                    if action == "End Program":
                        self.enter_design_mode(); return
                    elif action == "Loop Continuously":
                        self.pc = 0; self.wait_state = "IDLE"; return
                    elif action == "Jump to Step Line":
                        self.pc = max(0, step["flow_node"].target_step - 1); self.wait_state = "IDLE"; return
                self.pc += 1;
                self.wait_state = "IDLE";
                self.manual_pass_flag = False

    def new_file(self):
        self.studio_scene.clear();
        self.step_tree.clear();
        self.add_new_step_node()

    def add_new_step_node(self):
        QTreeWidgetItem(self.step_tree, ["", f"Step Block [{self.step_tree.topLevelItemCount() + 1}]", "",
                                         ""]); self.renumber_sequence()

    def insert_asset(self, item, name):
        p = self.get_active_step_parent();
        self.studio_scene.addItem(item);
        item.setPos(200, 200)
        ti = QTreeWidgetItem(p, ["", name, "Active", ""]);
        ti.setData(1, Qt.ItemDataRole.UserRole, item);
        self.renumber_sequence()

    def insert_clear_canvas(self):
        QTreeWidgetItem(self.get_active_step_parent(), ["", "🧹 Clear", "", ""]).setData(1, Qt.ItemDataRole.UserRole,
                                                                                        ClearCanvasData()); self.renumber_sequence()

    def insert_timer(self):
        QTreeWidgetItem(self.get_active_step_parent(), ["", "⏳ Timer", "", ""]).setData(1, Qt.ItemDataRole.UserRole,
                                                                                        TimerData()); self.renumber_sequence()

    def insert_observatory_node(self):
        QTreeWidgetItem(self.get_active_step_parent(), ["", "👁️ Obs", "", ""]).setData(1, Qt.ItemDataRole.UserRole,
                                                                                       ObservatoryNodeData()); self.renumber_sequence()

    def insert_vision_wait(self):
        QTreeWidgetItem(self.get_active_step_parent(), ["", "⏳ Wait Vision", "", ""]).setData(1,
                                                                                              Qt.ItemDataRole.UserRole,
                                                                                              VisionWaitData()); self.renumber_sequence()

    def insert_flow_control(self):
        QTreeWidgetItem(self.get_active_step_parent(), ["", "🛑 Flow", "", ""]).setData(1, Qt.ItemDataRole.UserRole,
                                                                                       FlowControlData()); self.renumber_sequence()

    def delete_selected_tree_node(self):
        sel = self.step_tree.selectedItems()
        if sel:
            asset = sel[0].data(1, Qt.ItemDataRole.UserRole)
            if isinstance(asset, QGraphicsItem): self.studio_scene.removeItem(asset)
            if sel[0].parent():
                sel[0].parent().removeChild(sel[0])
            else:
                self.step_tree.invisibleRootItem().removeChild(sel[0])
            self.renumber_sequence()


def main():
    app = QApplication(sys.argv);
    app.setStyleSheet(DARK_THEME)
    canvas = ProjectorCanvas();
    authoring = AuthoringInterface(canvas);
    authoring.show();
    sys.exit(app.exec())


if __name__ == "__main__": main()