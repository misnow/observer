import sys
import os
import json
import base64
import time
import subprocess

# Silence OpenCV C++ probing errors
os.environ["OPENCV_LOG_LEVEL"] = "FATAL"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"
os.environ["OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"] = "0"

import cv2
import numpy as np
from PyQt6.QtWidgets import *
from PyQt6.QtCore import QTimer, Qt, QSettings
from PyQt6.QtGui import QImage, QPixmap, QAction

# Import local modules
from vision_tools import (img_to_b64, b64_to_img, PerspectivePlaneROI, SearchROI,
                          evaluate_activation)
from ui_components import DARK_THEME, LLMWorker, VisionCanvasView, ThresholdScoreBar, LLMProviderSettingsWidget

# Try to import MediaPipe for Human Rigging
try:
    import mediapipe as mp

    mp_pose = mp.solutions.pose
    mp_drawing = mp.solutions.drawing_utils
    MP_AVAILABLE = True
except Exception as e:
    MP_AVAILABLE = False


class ObservatoryEngine(QMainWindow):
    def __init__(self, logger):
        super().__init__()
        self.logger = logger
        self.setWindowTitle("Observatory - Professional Vision Environment")
        self.setGeometry(150, 150, 1450, 850)

        # Initialize local storage for API keys and settings
        self.settings = QSettings("LightGuide", "Observatory")

        self.camera = None
        self.camera_index = 0
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.tool_count = 0
        self.current_raw_frame = None

        self.tool_states = {}
        self.tool_responses = {}
        self.tool_response_times = {}
        # Still frames grabbed on Studio's request, keyed by the requesting
        # Studio item's name: {"path": <png on disk>, "time": <epoch>}.
        # Studio compares the timestamp to know a capture is fresh, the same
        # way it distinguishes fresh LLM responses from stale ones.
        self.captures = {}
        self.world_calibration_matrix = None

        if MP_AVAILABLE:
            self.pose_tracker = mp_pose.Pose(min_detection_confidence=0.5, min_tracking_confidence=0.5)

        # --- IPC BRIDGE TIMERS ---
        # 1. Broadcaster: Writes the live vision states for Studio to read
        self.state_writer_timer = QTimer()
        self.state_writer_timer.timeout.connect(self.write_vision_state)
        self.state_writer_timer.start(100)

        # 2. Listener: Listens for Studio commanding it to load a new project
        self.last_command_timestamp = 0
        self.command_listener_timer = QTimer()
        self.command_listener_timer.timeout.connect(self.check_for_studio_commands)
        self.command_listener_timer.start(500)

        # 3. Watchdog: an LLM call (Gemini or local) occasionally hangs mid
        # network request and never returns, leaving the response box stuck
        # on "Processing image..." forever with no way to recover short of
        # restarting the app. This periodically checks how long any LLM
        # Vision tool has been processing and alarms/recovers it past a
        # user-configurable timeout (persisted, default 60s).
        self.llm_timeout_seconds = float(self.settings.value("llm_timeout_seconds", 60))
        self.llm_watchdog_timer = QTimer()
        self.llm_watchdog_timer.timeout.connect(self.check_llm_timeouts)
        self.llm_watchdog_timer.start(1000)

        self.setup_ui()
        self.build_menubar()

    def set_llm_timeout_seconds(self, seconds):
        self.llm_timeout_seconds = float(seconds)
        self.settings.setValue("llm_timeout_seconds", self.llm_timeout_seconds)

    def check_llm_timeouts(self):
        # This runs on a 1s QTimer - an uncaught exception escaping a
        # Qt-invoked slot gets escalated to a hard process abort by this
        # PyQt6 build instead of a catchable traceback, so this must be
        # exception-safe rather than letting a bad tool config take the
        # whole app down.
        try:
            now = time.time()
            for item in self.cam_scene.items():
                if not (getattr(item, 'tool_type', None) == "LLM Vision"
                        and getattr(item, 'llm_is_processing', False)
                        and not getattr(item, 'llm_timeout_alarmed', False)):
                    continue

                elapsed = now - item.llm_trigger_time
                if elapsed <= self.llm_timeout_seconds:
                    continue

                item.llm_timeout_alarmed = True
                item.llm_is_processing = False
                # Bump the generation counter (not force-kill the worker -
                # see the note on trigger_llm_tool/handle_llm_result for why):
                # any result the abandoned worker eventually produces will
                # carry the OLD generation number and get silently discarded
                # instead of overwriting a later retriggered call's result.
                item.llm_generation = getattr(item, 'llm_generation', 0) + 1
                alarm_msg = (f"ALARM: LLM Call '{item.tool_name}' was stuck on 'Processing image...' "
                             f"for over {self.llm_timeout_seconds:.0f}s - abandoned, ready to retry.")
                self.logger(f"⚠️ {alarm_msg}")
                item.llm_response = f"⚠️ {alarm_msg}"
                self.tool_responses[item.tool_name] = item.llm_response
                # A fresh response_time here means Studio's own separate
                # Hold-for-Response failsafe doesn't have to wait its own
                # full timeout again on top of this one - it sees this
                # alarm as "the response" and unblocks immediately.
                self.tool_response_times[item.tool_name] = now
                self._safe_set_llm_resp_box(item.llm_response)
        except Exception as e:
            self.logger(f"LLM watchdog error: {e}")

    def build_menubar(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")

        new_action = QAction("🆕 New Project", self)
        new_action.triggered.connect(self.new_project)
        file_menu.addAction(new_action)
        file_menu.addSeparator()

        open_action = QAction("📂 Open Project", self)
        open_action.triggered.connect(lambda: self.load_project(None))  # Pass None for manual dialog
        file_menu.addAction(open_action)

        save_action = QAction("💾 Save Project", self)
        save_action.triggered.connect(self.save_project)
        file_menu.addAction(save_action)

    def new_project(self):
        try:
            confirm = QMessageBox.question(
                self, "New Project",
                "Clear all tools and start a new Observatory project?\n\nAny unsaved work will be lost.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if confirm != QMessageBox.StandardButton.Yes:
                return

            if self.timer.isActive():
                self.toggle_camera()

            # Clear straight from the scene, not by walking
            # active_tools_list: that list only shows the camera currently
            # being viewed, so draining it would strand every other
            # camera's tools invisibly in the project.
            for item in list(self.cam_scene.items()):
                if hasattr(item, 'tool_type'):
                    self.cam_scene.removeItem(item)
            self.active_tools_list.blockSignals(True)
            self.active_tools_list.clear()
            self.active_tools_list.blockSignals(False)

            self.tool_count = 0
            self.tool_states.clear()
            self.tool_responses.clear()
            self.tool_response_times.clear()
            self.world_calibration_matrix = None
            self.current_raw_frame = None
            self.build_empty_properties()
            self.logger("New project started - all tools cleared.")
        except Exception as e:
            self.logger(f"Error starting new project: {e}")

    def write_vision_state(self):
        """IPC Protocol: Constantly broadcast tool states (and LLM Vision responses) for Studio."""
        # Written atomically: open(...,"w") truncates to zero length first,
        # and Studio polls this file every 50ms, so it regularly caught the
        # empty window and failed to parse ("Expecting value: line 1 column
        # 1"). Writing to a temp file and renaming means a reader always
        # sees either the previous complete state or the new one, never a
        # half-written file. os.replace is atomic on Windows and POSIX.
        try:
            tmp = "vision_state.json.tmp"
            with open(tmp, "w") as f:
                json.dump({"states": self.tool_states, "responses": self.tool_responses,
                           "response_times": self.tool_response_times,
                           "captures": self.captures}, f)
            os.replace(tmp, "vision_state.json")
        except Exception:
            pass  # transient FS contention; the next tick rewrites anyway

    def check_for_studio_commands(self):
        """IPC Protocol: Listen for file-load commands from Studio Engine."""
        if os.path.exists("studio_command.json"):
            try:
                with open("studio_command.json", "r") as f:
                    command = json.load(f)

                if command.get("timestamp", 0) > self.last_command_timestamp:
                    self.last_command_timestamp = command["timestamp"]

                    if command.get("action") == "load_and_start":
                        filepath = command.get("filepath")
                        if os.path.exists(filepath):
                            self.logger(f"IPC Command received: Loading {os.path.basename(filepath)}")
                            self.load_project(filepath)

                            # Automatically start camera if it isn't running
                            if not self.timer.isActive():
                                self.toggle_camera()

                    elif command.get("action") == "trigger_llm":
                        tool_name = command.get("tool_name")
                        prompt = command.get("prompt", "")
                        obs_file = command.get("observatory_file")
                        tool_item = self.find_tool_by_name(tool_name)

                        # The target tool isn't loaded yet - Studio's Line
                        # trigger fires the moment a step activates, with no
                        # guarantee Observatory already has the right project
                        # (or any project) open, e.g. right after a fresh
                        # project file was made. Load it ourselves before
                        # giving up on the tool being "not found".
                        just_started_camera = False
                        if not tool_item and obs_file and os.path.exists(obs_file):
                            self.logger(f"IPC Command: '{tool_name}' not loaded yet - loading {os.path.basename(obs_file)} first.")
                            self.load_project(obs_file)
                            if not self.timer.isActive():
                                self.toggle_camera()
                                just_started_camera = True
                            tool_item = self.find_tool_by_name(tool_name)

                        if tool_item and tool_item.tool_type == "LLM Vision":
                            if just_started_camera:
                                # The camera was only just switched on this
                                # tick - current_raw_frame won't be populated
                                # until update_frame's own 30ms timer actually
                                # captures a frame. Dispatching immediately
                                # here would silently skip with "no frame to
                                # analyze" and never retry, leaving Studio's
                                # Hold for Response waiting the full 60s
                                # failsafe for nothing.
                                self.logger(f"IPC Command: camera just started - deferring LLM Call on '{tool_name}' until a frame is available.")
                                QTimer.singleShot(150, lambda: self._dispatch_llm_trigger(tool_name, prompt))
                            else:
                                self._dispatch_llm_trigger(tool_name, prompt)
                        else:
                            self.logger(f"IPC Command error: LLM Vision tool '{tool_name}' not found.")

                    elif command.get("action") == "capture_image":
                        item_name = command.get("item_name", "capture")
                        obs_file = command.get("observatory_file")
                        # Same cold-start problem the LLM trigger has: Studio
                        # may fire this before Observatory has a project open
                        # or a camera running, so bring both up first rather
                        # than failing with "no frame".
                        if obs_file and os.path.exists(obs_file) and not self.cam_scene.items():
                            self.load_project(obs_file)
                        if not self.timer.isActive():
                            self.toggle_camera()
                        self._dispatch_capture(item_name)
            except Exception:
                pass

    def capture_tool_image(self, tool_item):
        """Grab a still for an Image Capture tool - ROI crop or full frame."""
        try:
            if self.current_raw_frame is None:
                self.logger(f"Capture skipped for '{tool_item.tool_name}': camera isn't running.")
                return None
            frame = self.current_raw_frame
            if getattr(tool_item, 'capture_mode', "ROI") == "ROI":
                x1 = int(max(0, tool_item.pos().x()))
                y1 = int(max(0, tool_item.pos().y()))
                x2 = int(min(frame.shape[1], x1 + tool_item.rect().width()))
                y2 = int(min(frame.shape[0], y1 + tool_item.rect().height()))
                if x2 <= x1 or y2 <= y1:
                    self.logger(f"Capture skipped for '{tool_item.tool_name}': ROI is off-frame.")
                    return None
                image = frame[y1:y2, x1:x2]
            else:
                image = frame

            capture_dir = os.path.join(os.getcwd(), "captures")
            os.makedirs(capture_dir, exist_ok=True)
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_item.tool_name)
            path = os.path.join(capture_dir, f"{safe}.png")
            cv2.imwrite(path, image)

            tool_item.captured_path = path
            tool_item.capture_time = time.time()
            # Broadcast so Studio can display whatever this tool captured,
            # keyed by tool name exactly like states/responses already are.
            self.captures[tool_item.tool_name] = {"path": path, "time": tool_item.capture_time}
            self.logger(f"Captured {tool_item.capture_mode.lower()} for '{tool_item.tool_name}' "
                        f"({image.shape[1]}x{image.shape[0]}).")
            self._safe_set_label('capture_status_label', os.path.basename(path))
            self._safe_set_path_field('capture_path_field', path)
            self.update_capture_thumbnail(tool_item)
            return path
        except Exception as e:
            self.logger(f"Capture error for '{tool_item.tool_name}': {e}")
            return None

    def segment_tool_capture(self, tool_item, backend="grabcut", target=""):
        """Segment an Image Capture tool's still into a transparent cutout.

        `target` names the subject for backends that take a text prompt
        (Gemini); the local backends ignore it.
        """
        try:
            import segmenter as _seg
            path = getattr(tool_item, 'captured_path', "")
            if not path or not os.path.exists(path):
                self._safe_set_label('segment_status_label', "Capture an image first.")
                return
            out_path = os.path.splitext(path)[0] + "_cutout.png"
            # An ROI capture is ALREADY cropped to the subject, so the
            # prompt box must hug the image edges. Falling back to the
            # generic centre box here forced the subject's own edges to be
            # treated as definite background and produced an empty mask.
            img = cv2.imread(path)
            if img is None:
                self._safe_set_label('segment_status_label', "Could not read the capture.")
                return
            h, w = img.shape[:2]
            if getattr(tool_item, 'capture_mode', "ROI") == "ROI":
                m = max(2, int(min(w, h) * 0.02))
                bbox = (m, m, w - 2 * m, h - 2 * m)
            else:
                bbox = (int(w * 0.1), int(h * 0.1), int(w * 0.8), int(h * 0.8))
            if backend == "gemini":
                self._safe_set_label('segment_status_label',
                                     "Uploading to Gemini and segmenting...")
                self.logger(f"Segmenting '{tool_item.tool_name}' with Gemini - "
                            f"the capture is uploaded to Google for this.")
            else:
                self._safe_set_label('segment_status_label', "Segmenting...")

            # The key is read straight from settings and handed to the worker;
            # it is never written into a project file or a log line.
            worker = _seg.SegmentWorker(
                path, out_path, bbox=bbox, backend=backend,
                api_key=str(self.settings.value("gemini_api_key",
                                                self.settings.value("api_key", "")) or ""),
                model=str(self.settings.value("gemini_segment_model",
                                              _seg.DEFAULT_GEMINI_MODEL) or
                          _seg.DEFAULT_GEMINI_MODEL),
                target=target)

            def done(result, error, it=tool_item, wk=worker):
                # Queued cross-thread slot: the properties panel may have been
                # rebuilt (clear_layout -> deleteLater) while this ran, so the
                # item's own state is updated first and separately, and every
                # widget touch is guarded. An exception escaping a Qt slot is
                # escalated to a hard abort by this PyQt6 build.
                if error:
                    self.logger(f"Segmentation failed for '{it.tool_name}': {error}")
                    self._safe_set_label('segment_status_label', str(error)[:80])
                else:
                    it.cutout_path = result["path"]
                    # Publish the cutout next to the raw capture so Studio can
                    # offer it as a selectable source. Previously the cutout
                    # never left Observatory, so a segmented image simply had
                    # no route onto the canvas. Data model first, widgets
                    # after - a torn-down panel must not cost us the result.
                    entry = self.captures.setdefault(it.tool_name, {})
                    entry["cutout_path"] = result["path"]
                    entry["cutout_time"] = time.time()
                    pct = result["coverage"] * 100
                    # A near-empty mask means the segmenter found nothing.
                    # Saying so beats handing over a blank PNG that only
                    # reveals itself as useless further down the pipeline.
                    if pct < 1.0:
                        self.logger(f"⚠️ Segmentation of '{it.tool_name}' found almost no subject "
                                    f"({pct:.1f}% coverage) - the cutout is effectively empty. "
                                    f"Try a tighter ROI or a different backend.")
                        self._safe_set_label('segment_status_label',
                                             f"⚠️ Empty result ({pct:.1f}%) - subject not found")
                    else:
                        self.logger(f"Segmented '{it.tool_name}' -> {os.path.basename(result['path'])} "
                                    f"({pct:.1f}% coverage)")
                        self._safe_set_label('segment_status_label',
                                             f"{os.path.basename(result['path'])} ({pct:.1f}%)")
                    # Both guarded internally; safe if the panel is long gone.
                    self.update_cutout_thumbnail(it)
                    self._safe_set_path_field('cutout_path_field', it.cutout_path)
                try:
                    if wk in self._seg_workers:
                        self._seg_workers.remove(wk)
                except Exception:
                    pass

            worker.finished_ok.connect(done)
            # A list, not a single attribute: a second run must not drop the
            # reference to a QThread that is still executing.
            if not hasattr(self, '_seg_workers'):
                self._seg_workers = []
            self._seg_workers.append(worker)
            worker.start()
        except Exception as e:
            self.logger(f"Segmentation error for '{tool_item.tool_name}': {e}")

    def _safe_set_label(self, attr, text):
        try:
            lbl = getattr(self, attr, None)
            if lbl is not None:
                lbl.setText(text)
        except RuntimeError:
            pass   # panel rebuilt; the underlying C++ widget is gone

    def update_capture_thumbnail(self, tool_item):
        self._set_thumbnail('capture_thumb_label',
                            getattr(tool_item, 'captured_path', ""), "No capture")

    def update_cutout_thumbnail(self, tool_item):
        self._set_thumbnail('cutout_thumb_label',
                            getattr(tool_item, 'cutout_path', ""), "Not segmented")

    def _set_thumbnail(self, attr, path, empty_text):
        """Shared thumbnail painter for the capture and cutout previews.

        Guarded like every other worker-reachable widget touch: the
        properties panel is rebuilt constantly, so this QLabel may already be
        a destroyed C++ object by the time a segmentation finishes.
        """
        try:
            lbl = getattr(self, attr, None)
            if lbl is None:
                return
            pix = QPixmap(path) if path and os.path.exists(path) else QPixmap()
            if not pix.isNull():
                lbl.setPixmap(pix.scaled(160, 120, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
            else:
                lbl.clear()
                lbl.setText(empty_text)
        except RuntimeError:
            pass

    def _make_path_row(self, path):
        """Read-only, selectable *full* path plus a button that reveals it in
        Explorer.

        Showing only the basename left no way to tell where a capture or
        cutout had actually been written - which matters here because the
        location depends on the process working directory, not the project
        file. Returns (row_widget, field) so callers can keep the field to
        update later.
        """
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        field = QLineEdit(path or "")
        field.setReadOnly(True)
        field.setPlaceholderText("(none yet)")
        field.setToolTip(path or "")
        field.setCursorPosition(0)
        lay.addWidget(field)
        btn = QPushButton("📂")
        btn.setFixedWidth(32)
        btn.setToolTip("Reveal in Explorer")
        btn.setEnabled(bool(path) and os.path.exists(path))
        btn.clicked.connect(lambda _=False, p=path: self.reveal_in_explorer(p))
        lay.addWidget(btn)
        return row, field

    def reveal_in_explorer(self, path):
        try:
            if path and os.path.exists(path):
                subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
            else:
                self.logger("Nothing to reveal - that file doesn't exist yet.")
        except Exception as e:
            self.logger(f"Could not open folder: {e}")

    @staticmethod
    def _hint(text):
        """An explanatory label that wraps.

        QLabel does NOT wrap by default, and an unwrapped label reports its
        full single-line width as its *minimum* size hint. One long sentence
        therefore sets the floor for the entire inspector column, and the
        camera view is what pays for it. Every help text in this panel goes
        through here.
        """
        label = QLabel(text)
        label.setWordWrap(True)
        return label

    def _refresh_activation_label(self, tool_item):
        """Show how the last reply scored, guarded like every panel touch."""
        try:
            label = getattr(self, 'llm_activation_label', None)
            if label is None:
                return
            mode = getattr(tool_item, 'activation_mode', "Off")
            if mode == "Off":
                label.setText("<span style='color:#888;'>Token scoring is off - "
                              "any reply scores 100.</span>")
            elif getattr(tool_item, 'activation_hit', False):
                label.setText(f"<span style='color:#2ecc71;'><b>ACTIVATED</b> - "
                              f"score {tool_item.current_score}</span>")
            else:
                label.setText(f"<span style='color:#e67e22;'>not activated - "
                              f"score {tool_item.current_score}</span>")
        except RuntimeError:
            pass

    def _safe_text(self, widget):
        """Read a line edit that the properties panel may already have torn down."""
        try:
            return widget.text().strip()
        except RuntimeError:
            return ""

    def _safe_set_path_field(self, attr, path):
        try:
            field = getattr(self, attr, None)
            if field is not None:
                field.setText(path or "")
                field.setToolTip(path or "")
                field.setCursorPosition(0)
        except RuntimeError:
            pass

    def _dispatch_capture(self, item_name, retries_left=25):
        """Grab the current camera frame and write it out for Studio.

        Retries because the camera may have only just been switched on -
        current_raw_frame stays None until update_frame's own timer actually
        reads a frame, and giving up immediately would leave Studio waiting
        on a capture that never arrives.
        """
        try:
            if self.current_raw_frame is None:
                if retries_left > 0:
                    QTimer.singleShot(150, lambda: self._dispatch_capture(item_name, retries_left - 1))
                else:
                    self.logger(f"Capture failed for '{item_name}': camera never produced a frame.")
                return

            capture_dir = os.path.join(os.getcwd(), "captures")
            os.makedirs(capture_dir, exist_ok=True)
            # Deterministic per-item filename: re-capturing replaces the
            # previous still rather than littering the folder, and Studio
            # can find it without parsing anything.
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in item_name)
            path = os.path.join(capture_dir, f"{safe}.png")
            cv2.imwrite(path, self.current_raw_frame)
            self.captures[item_name] = {"path": path, "time": time.time()}
            self.logger(f"Captured still for '{item_name}' -> {os.path.basename(path)}")
        except Exception as e:
            self.logger(f"Capture error for '{item_name}': {e}")

    def _dispatch_llm_trigger(self, tool_name, prompt, retries_left=25):
        try:
            tool_item = self.find_tool_by_name(tool_name)
            if not tool_item or tool_item.tool_type != "LLM Vision":
                self.logger(f"IPC Command error: LLM Vision tool '{tool_name}' not found.")
                return
            if self.current_raw_frame is None:
                if retries_left > 0:
                    QTimer.singleShot(150, lambda: self._dispatch_llm_trigger(tool_name, prompt, retries_left - 1))
                else:
                    self.logger(f"IPC Command error: camera never produced a frame for LLM Call '{tool_name}'.")
                return
            tool_item.llm_prompt = prompt
            self.logger(f"IPC Command received: Running LLM Call on '{tool_name}'.")
            self.trigger_llm_tool(tool_item)
            if tool_item in self.cam_scene.selectedItems():
                self.load_tool_properties_to_ui(tool_item)
        except Exception as e:
            self.logger(f"IPC Error dispatching LLM trigger: {e}")

    def find_tool_by_name(self, tool_name):
        for item in self.cam_scene.items():
            if hasattr(item, 'tool_name') and item.tool_name == tool_name:
                return item
        return None

    def setup_ui(self):
        main_layout = QVBoxLayout()
        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Camera Device: "))
        self.camera_selector = QComboBox()
        self.populate_cameras()
        # Switching which camera you're viewing swaps which camera's tools
        # are shown/evaluated, without disturbing the others, and moves the
        # live feed itself to the newly selected device.
        self.camera_selector.currentIndexChanged.connect(
            lambda _: self.on_camera_selection_changed())
        toolbar.addWidget(self.camera_selector)

        self.btn_toggle_cam = QPushButton("Start Camera Feed")
        self.btn_toggle_cam.clicked.connect(self.toggle_camera)
        toolbar.addWidget(self.btn_toggle_cam)
        toolbar.addStretch()
        main_layout.addLayout(toolbar)

        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(QLabel("<b>Vision Tool Arsenal</b>"))

        self.tool_selector = QComboBox()
        available_tools = ["Image Capture", "Pattern Match", "Blob Detection", "Delete (Missing Object)", "Motion Detection",
                           "Reference Plane", "LLM Vision"]
        if MP_AVAILABLE: available_tools.append("Human Rigging (Pose)")
        self.tool_selector.addItems(available_tools)
        left_layout.addWidget(self.tool_selector)
        left_layout.addWidget(QLabel("<small><i>Double-click video to place tool.</i></small>"))

        self.active_tools_list = QListWidget()
        self.active_tools_list.itemChanged.connect(self.handle_tool_rename)
        self.active_tools_list.itemSelectionChanged.connect(self.sync_list_to_scene)
        left_layout.addWidget(self.active_tools_list)

        self.btn_delete_tool = QPushButton("🗑️ Delete Selected Tool")
        self.btn_delete_tool.clicked.connect(self.delete_selected_tool)
        left_layout.addWidget(self.btn_delete_tool)
        h_splitter.addWidget(left_panel)

        self.cam_scene = QGraphicsScene()
        self.cam_scene.selectionChanged.connect(self.sync_scene_to_list)
        self.cam_view = VisionCanvasView(self.cam_scene)
        self.cam_view.setStyleSheet("background-color: #1e1e1e; border: 1px solid #555;")
        self.cam_view.double_clicked.connect(self.spawn_tool_roi)
        self.video_frame_item = self.cam_scene.addPixmap(QPixmap())
        h_splitter.addWidget(self.cam_view)

        right_panel = QWidget()
        self.right_layout = QVBoxLayout(right_panel)

        # --- GLOBAL SETTINGS (LLM PROVIDER) ---
        # Collapsible, and collapsed by default. Provider/key/timeout are set
        # once and then rarely touched, but they sit above every LLM tool's own
        # settings - on the tool that already has the tallest panel in the app.
        self.global_group = QGroupBox("Global Configuration  (click to expand)")
        self.global_group.setCheckable(True)
        self.global_group.setChecked(False)
        global_outer = QVBoxLayout(self.global_group)
        self.global_body = QWidget()
        global_lay = QFormLayout(self.global_body)
        global_outer.addWidget(self.global_body)
        self.global_body.setVisible(False)
        self.global_group.toggled.connect(
            lambda on: self.global_body.setVisible(bool(on)))

        self.llm_settings = LLMProviderSettingsWidget(self.settings)
        global_lay.addRow(self.llm_settings)

        self.spin_llm_timeout = QSpinBox()
        self.spin_llm_timeout.setRange(5, 600)
        self.spin_llm_timeout.setSuffix(" sec")
        self.spin_llm_timeout.setValue(int(self.llm_timeout_seconds))
        self.spin_llm_timeout.valueChanged.connect(self.set_llm_timeout_seconds)
        global_lay.addRow("LLM Hang Timeout:", self.spin_llm_timeout)

        self.right_layout.addWidget(self.global_group)

        self.right_layout.addWidget(QLabel("<b>Tool Inspector</b>"))

        self.filter_group = QGroupBox("Pre-Processing Filters")
        self.filter_layout = QFormLayout(self.filter_group)
        self.right_layout.addWidget(self.filter_group)

        # No ampersand: Qt reads "&" in a title as a keyboard mnemonic, eats it,
        # and underlines the next character - the title rendered as
        # "Tool Settings  Output" with a stray gap.
        self.tool_prop_group = QGroupBox("Tool Settings / Output")
        self.tool_prop_layout = QFormLayout(self.tool_prop_group)
        self.right_layout.addWidget(self.tool_prop_group)

        self.score_group = QGroupBox("Dynamic Trigger Output")
        score_layout = QVBoxLayout(self.score_group)
        self.score_bar = ThresholdScoreBar()
        self.score_bar.trigger_state_changed.connect(self.sync_threshold_to_tool)
        score_layout.addWidget(self.score_bar)
        self.right_layout.addWidget(self.score_group)

        self.right_layout.addStretch()

        # The inspector SCROLLS rather than grows. Tool panels vary enormously
        # in height - an LLM Vision tool carries a prompt box, a response box,
        # activation settings and the provider config - and without this the
        # panel simply demanded more room than the window had, and the camera
        # view was what gave way.
        self.right_scroll = QScrollArea()
        self.right_scroll.setWidget(right_panel)
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.right_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Horizontal scrolling is deliberately OFF: content that is too wide
        # should wrap, not slide sideways out of view. Combined with the width
        # cap this is what stops one wide widget dictating the column width.
        self.right_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.right_scroll.setMinimumWidth(300)
        self.right_scroll.setMaximumWidth(460)
        h_splitter.addWidget(self.right_scroll)

        # The video feed is the pane that must never be squeezed out - it is
        # the whole point of the window, and you cannot place or judge an ROI
        # you cannot see. A hard minimum plus setCollapsible(False) means no
        # amount of properties-panel content can encroach on it, and the
        # stretch factors send any spare width here rather than to the panels.
        left_panel.setMaximumWidth(320)
        self.cam_view.setMinimumWidth(420)
        self.cam_view.setMinimumHeight(320)
        h_splitter.setCollapsible(1, False)
        h_splitter.setStretchFactor(0, 0)
        h_splitter.setStretchFactor(1, 1)
        h_splitter.setStretchFactor(2, 0)
        h_splitter.setSizes([250, 900, 400])
        main_layout.addWidget(h_splitter)

        self.current_thumb_label = None
        self.build_empty_properties()

    def build_empty_properties(self):
        self.clear_layout(self.filter_layout)
        self.clear_layout(self.tool_prop_layout)
        self.filter_group.setVisible(False)
        self.score_group.setVisible(False)
        self.global_group.setVisible(False)
        self.tool_prop_layout.addRow(self._hint("<i>Select a tool to view properties.</i>"))

    def clear_layout(self, layout):
        # takeAt() only removes a widget from LAYOUT MANAGEMENT - it stays
        # parented and keeps painting at its old geometry until deleteLater()
        # is actually serviced on the next event-loop pass. Rebuilding this
        # panel therefore drew the outgoing widgets on top of the incoming
        # ones, which is why a freshly selected tool could show two labels
        # overlapping and half-illegible.
        #
        # hide() + setParent(None) removes it from the paint tree at once;
        # deleteLater() still does the actual destruction, because deleting
        # synchronously here would be unsafe when this is called from inside a
        # widget's own signal handler.
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                widget = item.widget()
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout():
                self.clear_layout(item.layout())
        self.current_thumb_label = None

    def sync_threshold_to_tool(self, triggered):
        items = self.cam_scene.selectedItems()
        if items and hasattr(items[0], 'sensitivity'): items[0].sensitivity = self.score_bar.threshold

    def update_filter_val(self, item, attr, val):
        setattr(item, attr, val)
        self.reapply_static_filters(item)

    def update_tool_val(self, item, attr, val):
        setattr(item, attr, val)

    def reapply_static_filters(self, item):
        if item.raw_trained_template is not None:
            item.trained_template = self.apply_filters(item, item.raw_trained_template)
            self.update_template_thumbnail(item)
        if item.raw_reference_frame is not None:
            item.reference_frame = self.apply_filters(item, item.raw_reference_frame)

    def update_template_thumbnail(self, item):
        if self.current_thumb_label is None: return
        if item.trained_template is not None:
            rgb_crop = cv2.cvtColor(item.trained_template, cv2.COLOR_GRAY2RGB)
            h, w, ch = rgb_crop.shape
            img = QImage(rgb_crop.data, w, h, ch * w, QImage.Format.Format_RGB888)
            self.current_thumb_label.setPixmap(
                QPixmap.fromImage(img).scaled(100, 60, Qt.AspectRatioMode.KeepAspectRatio))
        else:
            self.current_thumb_label.clear()
            self.current_thumb_label.setText("No Trained Image")

    def create_horiz_slider_row(self, item, bool_attr, val_attr, checkbox_label, slider_range):
        lay = QHBoxLayout()
        chk = QCheckBox(checkbox_label)
        chk.setChecked(getattr(item, bool_attr))
        chk.toggled.connect(lambda v, i=item, b=bool_attr: self.update_filter_val(i, b, v))
        sld = QSlider(Qt.Orientation.Horizontal)
        sld.setRange(slider_range[0], slider_range[1])
        sld.setValue(getattr(item, val_attr))
        sld.valueChanged.connect(lambda v, i=item, a=val_attr: self.update_filter_val(i, a, v))
        lay.addWidget(chk)
        lay.addWidget(sld)
        return lay

    def load_tool_properties_to_ui(self, tool_item):
        self.clear_layout(self.filter_layout)
        self.clear_layout(self.tool_prop_layout)
        self.filter_group.setVisible(True)
        self.score_group.setVisible(True)
        # Global Configuration (LLM provider + hang timeout) only matters for
        # LLM Vision tools - keeping it visible unconditionally for every
        # tool type ate into the vertical space every other tool's
        # properties (Blob Detection especially, with its own trained-
        # pattern controls) needed in this same panel.
        self.global_group.setVisible(tool_item.tool_type == "LLM Vision")

        self.score_bar.blockSignals(True)
        self.score_bar.threshold = tool_item.sensitivity
        self.score_bar.set_score(tool_item.current_score)
        self.score_bar.blockSignals(False)

        type_label = QLabel(f"<b>{tool_item.tool_type}</b>")
        type_label.setStyleSheet("color: #3498db;")
        self.tool_prop_layout.addRow("Tool Logic:", type_label)

        cb_camera = QComboBox()
        for i in range(self.camera_selector.count()):
            cb_camera.addItem(self.camera_selector.itemText(i), self.camera_selector.itemData(i))
        cam_idx = cb_camera.findData(getattr(tool_item, 'camera_index', 0))
        if cam_idx >= 0:
            cb_camera.setCurrentIndex(cam_idx)
        cb_camera.currentIndexChanged.connect(
            lambda _, i=tool_item, c=cb_camera: self.reassign_tool_camera(i, c.currentData()))
        self.tool_prop_layout.addRow("Camera:", cb_camera)

        if tool_item.tool_type == "Reference Plane":
            self.filter_group.setVisible(False)
            self.score_group.setVisible(False)

            spin_d12 = QDoubleSpinBox()
            spin_d12.setRange(0.1, 1000.0)
            spin_d12.setValue(tool_item.d1_2)
            spin_d12.valueChanged.connect(lambda v, i=tool_item: setattr(i, 'd1_2', v))
            self.tool_prop_layout.addRow("Dist 1->2 (in):", spin_d12)

            spin_d23 = QDoubleSpinBox()
            spin_d23.setRange(0.1, 1000.0)
            spin_d23.setValue(tool_item.d2_3)
            spin_d23.valueChanged.connect(lambda v, i=tool_item: setattr(i, 'd2_3', v))
            self.tool_prop_layout.addRow("Dist 2->3 (in):", spin_d23)

            spin_d34 = QDoubleSpinBox()
            spin_d34.setRange(0.1, 1000.0)
            spin_d34.setValue(tool_item.d3_4)
            spin_d34.valueChanged.connect(lambda v, i=tool_item: setattr(i, 'd3_4', v))
            self.tool_prop_layout.addRow("Dist 3->4 (in):", spin_d34)

            spin_d41 = QDoubleSpinBox()
            spin_d41.setRange(0.1, 1000.0)
            spin_d41.setValue(tool_item.d4_1)
            spin_d41.valueChanged.connect(lambda v, i=tool_item: setattr(i, 'd4_1', v))
            self.tool_prop_layout.addRow("Dist 4->1 (in):", spin_d41)

            btn_calc = QPushButton("📐 Calibrate World Matrix")
            btn_calc.setStyleSheet("background-color: #f39c12; font-weight: bold; color: black;")
            btn_calc.clicked.connect(lambda: self.calculate_homography(tool_item))
            self.tool_prop_layout.addRow(btn_calc)

            status = QLabel("Active" if self.world_calibration_matrix is not None else "Pending Calculation")
            status.setStyleSheet("color: #2ecc71;" if self.world_calibration_matrix is not None else "color: #e74c3c;")
            self.tool_prop_layout.addRow("Matrix Status:", status)
            return

        self.filter_layout.addRow(
            self.create_horiz_slider_row(tool_item, 'use_thresh', 'filter_thresh', "Threshold", (0, 255)))
        self.filter_layout.addRow(
            self.create_horiz_slider_row(tool_item, 'use_depth', 'filter_depth', "Depth Blur", (1, 15)))
        self.filter_layout.addRow(
            self.create_horiz_slider_row(tool_item, 'use_edge', 'filter_edge', "Edge Detect", (10, 200)))

        if tool_item.tool_type == "Pattern Match":
            btn_train = QPushButton("🎯 Train Pattern")
            btn_train.clicked.connect(lambda: self.train_pattern_tool(tool_item))
            self.tool_prop_layout.addRow(btn_train)

            self.current_thumb_label = QLabel("No Trained Image")
            self.current_thumb_label.setFixedSize(100, 60)
            self.current_thumb_label.setStyleSheet("background-color: #1e1e1e; border: 1px dashed #555;")
            self.tool_prop_layout.addRow("Template Preview:", self.current_thumb_label)
            self.update_template_thumbnail(tool_item)

        elif tool_item.tool_type in ["Motion Detection", "Delete (Missing Object)"]:
            slider_thresh = QSlider(Qt.Orientation.Horizontal)
            slider_thresh.setRange(1, 255)
            slider_thresh.setValue(tool_item.threshold)
            lbl_thresh_val = QLabel(str(tool_item.threshold))
            lbl_thresh_val.setFixedWidth(32)
            row_thresh = QWidget()
            lay_thresh = QHBoxLayout(row_thresh)
            lay_thresh.setContentsMargins(0, 0, 0, 0)
            lay_thresh.addWidget(slider_thresh)
            lay_thresh.addWidget(lbl_thresh_val)

            def _on_thresh_changed(v, i=tool_item, lbl=lbl_thresh_val):
                self.update_tool_val(i, 'threshold', v)
                try:
                    lbl.setText(str(v))
                except RuntimeError:
                    pass   # panel rebuilt under us

            slider_thresh.valueChanged.connect(_on_thresh_changed)
            self.tool_prop_layout.addRow("Pixel Diff Thresh:", row_thresh)
            self.tool_prop_layout.addRow(self._hint(
                "<i>How far a pixel must change to count as motion (1-255). "
                "Lower = more sensitive. Real inter-frame deltas are usually "
                "10-40, so values much above ~60 will rarely trigger.</i>"))

            slider_area = QSlider(Qt.Orientation.Horizontal)
            slider_area.setRange(10, 5000)
            slider_area.setValue(tool_item.min_area)
            slider_area.valueChanged.connect(lambda v, i=tool_item: self.update_tool_val(i, 'min_area', v))
            self.tool_prop_layout.addRow("Min Motion Area:", slider_area)

            btn_ref = QPushButton("📷 Capture Reference State")
            btn_ref.setStyleSheet("background-color: #c0392b; font-weight: bold;")
            btn_ref.clicked.connect(lambda: self.set_motion_reference(tool_item))
            self.tool_prop_layout.addRow(btn_ref)

            chk_auto = QCheckBox("Auto-capture on camera start")
            chk_auto.setChecked(tool_item.auto_capture_reference)
            chk_auto.toggled.connect(lambda v, i=tool_item: setattr(i, 'auto_capture_reference', v))
            self.tool_prop_layout.addRow(chk_auto)

            status = QLabel("Ready" if tool_item.reference_frame is not None else "Missing Reference Frame")
            self.tool_prop_layout.addRow("Status:", status)

        elif tool_item.tool_type == "LLM Vision":
            txt_prompt = QTextEdit()
            txt_prompt.setPlainText(tool_item.llm_prompt)
            txt_prompt.textChanged.connect(lambda i=tool_item, t=txt_prompt: setattr(i, 'llm_prompt', t.toPlainText()))
            self.tool_prop_layout.addRow("LLM Prompt:", txt_prompt)

            self.tool_prop_layout.addRow(QLabel("<hr><b>Activation Token</b>"))
            cb_act = QComboBox()
            for label, data in (("Off - any reply scores 100", "Off"),
                                ("Exact reply is the token (most reliable)", "Exact reply"),
                                ("Ends with token", "Ends with"),
                                ("Contains token anywhere", "Contains")):
                cb_act.addItem(label, data)
            idx = cb_act.findData(getattr(tool_item, 'activation_mode', "Off"))
            cb_act.setCurrentIndex(max(0, idx))
            cb_act.currentIndexChanged.connect(
                lambda _=0, i=tool_item, c=cb_act: setattr(i, 'activation_mode',
                                                           c.currentData()))
            self.tool_prop_layout.addRow("Scoring:", cb_act)

            txt_token = QLineEdit(getattr(tool_item, 'activation_token', "$ACTIVE"))
            txt_token.textChanged.connect(
                lambda t, i=tool_item: setattr(i, 'activation_token', t.strip()))
            self.tool_prop_layout.addRow("Token:", txt_token)

            self.tool_prop_layout.addRow(self._hint(
                "<i>Seeing the token drives this tool's score to 100 (otherwise "
                "0), which sets its state for a Studio <b>Vision Wait</b>.<br><br>"
                "Most reliable — <b>Exact reply</b> with a prompt that allows "
                "nothing else:<br>"
                "&nbsp;&nbsp;<tt>Is there a person in this room? Reply with only "
                "$ACTIVE if yes, or only NONE if no.</tt><br><br>"
                "<b>Ends with</b> suits the natural phrasing (<tt>...end your "
                "reply with $ACTIVE</tt>) but cannot fully separate a refusal: "
                "<i>\"no person, so I will not output $ACTIVE.\"</i> genuinely "
                "ends with the token. A negation guard catches the common "
                "phrasings; Exact reply has nothing to guard against.<br>"
                "<b>Contains</b> is loosest and fires on any mention.</i>"))

            self.llm_activation_label = QLabel()
            self._refresh_activation_label(tool_item)
            self.tool_prop_layout.addRow("Last result:", self.llm_activation_label)

            btn_send = QPushButton("🧠 Trigger LLM Query")
            btn_send.setStyleSheet("background-color: #9b59b6; font-weight: bold;")
            btn_send.clicked.connect(lambda: self.trigger_llm_tool(tool_item))
            self.tool_prop_layout.addRow(btn_send)

            txt_resp = QTextEdit()
            txt_resp.setReadOnly(True)
            txt_resp.setPlainText(tool_item.llm_response)
            self.llm_resp_box = txt_resp
            self.tool_prop_layout.addRow("LLM Response:", txt_resp)

        elif tool_item.tool_type == "Image Capture":
            cb_mode = QComboBox()
            cb_mode.addItems(["ROI", "Full Frame"])
            cb_mode.setCurrentText(getattr(tool_item, 'capture_mode', "ROI"))
            cb_mode.currentTextChanged.connect(
                lambda v, i=tool_item: self.update_tool_val(i, 'capture_mode', v))
            self.tool_prop_layout.addRow("Capture Mode:", cb_mode)
            self.tool_prop_layout.addRow(self._hint(
                "<i>ROI captures just the box; Full Frame captures the whole camera image. "
                "Drag/resize the box on the feed to set the region.</i>"))

            btn_capture = QPushButton("📷 Capture Now")
            btn_capture.setStyleSheet("background-color: #f39c12; font-weight: bold; color: black;")
            btn_capture.clicked.connect(lambda _=False, i=tool_item: self.capture_tool_image(i))
            self.tool_prop_layout.addRow(btn_capture)

            self.capture_status_label = QLabel(
                os.path.basename(tool_item.captured_path) if getattr(tool_item, 'captured_path', "")
                else "<i>No capture yet</i>")
            self.capture_status_label.setStyleSheet("color: #2ecc71;")
            self.tool_prop_layout.addRow("Captured:", self.capture_status_label)

            cap_row, self.capture_path_field = self._make_path_row(
                getattr(tool_item, 'captured_path', ""))
            self.tool_prop_layout.addRow("File:", cap_row)

            self.capture_thumb_label = QLabel()
            self.capture_thumb_label.setFixedSize(160, 120)
            self.capture_thumb_label.setStyleSheet("background-color: #1e1e1e; border: 1px dashed #555;")
            self.tool_prop_layout.addRow("Preview:", self.capture_thumb_label)
            self.update_capture_thumbnail(tool_item)

            self.tool_prop_layout.addRow(QLabel("<hr><b>AI Segmenter</b>"))
            import segmenter as _seg
            has_key = bool(self.settings.value("gemini_api_key",
                                               self.settings.value("api_key", "")))
            cb_backend = QComboBox()
            for name, (ok, msg) in _seg.backend_status(api_key="x" if has_key else "").items():
                cb_backend.addItem(f"{name}{'' if ok else '  (unavailable)'}", name)
                cb_backend.setItemData(cb_backend.count() - 1, msg, Qt.ItemDataRole.ToolTipRole)
            self.tool_prop_layout.addRow("Backend:", cb_backend)

            self.segment_target_input = QLineEdit()
            self.segment_target_input.setPlaceholderText(
                "e.g. the bracket  (Gemini only; blank = main foreground object)")
            self.tool_prop_layout.addRow("Subject:", self.segment_target_input)
            self.tool_prop_layout.addRow(self._hint(
                "<i>grabcut runs locally. <b>gemini uploads this capture to Google</b> "
                "and uses your AI Studio API key.</i>"))

            self.segment_status_label = QLabel(
                os.path.basename(tool_item.cutout_path) if getattr(tool_item, 'cutout_path', "")
                else "<i>Not segmented</i>")
            self.segment_status_label.setWordWrap(True)
            self.tool_prop_layout.addRow("Cutout:", self.segment_status_label)

            cut_row, self.cutout_path_field = self._make_path_row(
                getattr(tool_item, 'cutout_path', ""))
            self.tool_prop_layout.addRow("File:", cut_row)

            self.cutout_thumb_label = QLabel()
            self.cutout_thumb_label.setFixedSize(160, 120)
            self.cutout_thumb_label.setStyleSheet(
                "background-color: #1e1e1e; border: 1px dashed #555;")
            self.tool_prop_layout.addRow("Cutout Preview:", self.cutout_thumb_label)
            self.update_cutout_thumbnail(tool_item)

            btn_seg = QPushButton("✂️ Segment Capture")
            btn_seg.setStyleSheet("background-color: #16a085; font-weight: bold;")
            btn_seg.clicked.connect(
                lambda _=False, i=tool_item, c=cb_backend, t=self.segment_target_input:
                self.segment_tool_capture(i, c.currentData(), self._safe_text(t)))
            self.tool_prop_layout.addRow(btn_seg)

        elif tool_item.tool_type == "Blob Detection":
            cb_color = QComboBox()
            cb_color.addItems(["Dark Blobs (0)", "Light Blobs (255)"])
            cb_color.setCurrentIndex(0 if tool_item.blob_color == 0 else 1)
            cb_color.currentIndexChanged.connect(
                lambda idx, i=tool_item: self.update_tool_val(i, 'blob_color', 0 if idx == 0 else 255))
            self.tool_prop_layout.addRow("Target Color:", cb_color)

            slider_min_area = QSlider(Qt.Orientation.Horizontal)
            slider_min_area.setRange(1, 5000)
            slider_min_area.setValue(tool_item.threshold)
            slider_min_area.valueChanged.connect(lambda v, i=tool_item: self.update_tool_val(i, 'threshold', v))
            self.tool_prop_layout.addRow("Min Area:", slider_min_area)

            slider_max_area = QSlider(Qt.Orientation.Horizontal)
            slider_max_area.setRange(100, 20000)
            slider_max_area.setValue(tool_item.blob_max_area)
            slider_max_area.valueChanged.connect(lambda v, i=tool_item: self.update_tool_val(i, 'blob_max_area', v))
            self.tool_prop_layout.addRow("Max Area:", slider_max_area)

            self.blob_coord_label = QLabel(
                f"X: {tool_item.last_blob_x} px | Y: {tool_item.last_blob_y} px\nAngle: {tool_item.last_blob_a:.1f}°")
            self.blob_coord_label.setStyleSheet("color: #2ecc71; font-weight: bold;")
            self.tool_prop_layout.addRow("From Origin (px):", self.blob_coord_label)

            self.tool_prop_layout.addRow(self._hint("<i>To move origin, drag the green crosshair inside the ROI.</i>"))

    def trigger_llm_tool(self, tool_item):
        if tool_item.llm_is_processing:
            return
        if self.current_raw_frame is None:
            self.logger(
                f"LLM Call '{tool_item.tool_name}' skipped: camera feed isn't running yet, so there's no frame to analyze.")
            return

        # Validate that the LLM provider is configured before triggering the worker
        config = self.llm_settings.get_config()
        config_ready = (config["provider"] == "Gemini" and config.get("api_key")) or \
                        (config["provider"] == "OpenAI-Compatible" and config.get("base_url") and config.get("model"))
        if not config_ready:
            tool_item.llm_response = "Error: Please configure an LLM provider in the Global Configuration panel first."
            self._safe_set_llm_resp_box(tool_item.llm_response)
            return

        x1, y1 = int(max(0, tool_item.pos().x())), int(max(0, tool_item.pos().y()))
        x2, y2 = int(min(self.current_raw_frame.shape[1], x1 + tool_item.rect().width())), int(
            min(self.current_raw_frame.shape[0], y1 + tool_item.rect().height()))
        if x2 <= x1 or y2 <= y1: return

        crop = self.current_raw_frame[y1:y2, x1:x2]
        b64 = img_to_b64(crop)

        tool_item.llm_is_processing = True
        tool_item.llm_response = "Processing image..."
        tool_item.llm_trigger_time = time.time()
        tool_item.llm_timeout_alarmed = False
        # Bumped on every real trigger. A worker that gets abandoned by the
        # watchdog (see check_llm_timeouts) is deliberately never killed -
        # QThread.terminate() on a thread blocked inside urllib/OpenSSL can
        # corrupt shared C-library state and crash the WHOLE process later,
        # nowhere near the original call site. Instead, handle_llm_result
        # compares the generation it was launched with against the tool's
        # current one and silently drops results from an abandoned worker
        # that eventually does finish, rather than ever needing to kill it.
        tool_item.llm_generation = getattr(tool_item, 'llm_generation', 0) + 1
        my_generation = tool_item.llm_generation
        self._safe_set_llm_resp_box(tool_item.llm_response)

        try:
            # Keep the worker referenced on the tool item itself (not `self`) so that
            # triggering a second tool while this one is still running can't drop the
            # only reference to a live QThread and crash the process.
            tool_item.llm_worker = LLMWorker(config, tool_item.llm_prompt, b64)
            tool_item.llm_worker.finished.connect(
                lambda resp, gen=my_generation: self.handle_llm_result(tool_item, resp, gen))
            tool_item.llm_worker.start()
        except Exception as e:
            self.handle_llm_result(tool_item, f"Worker Init Error: {e}", my_generation)

    def _safe_set_llm_resp_box(self, text):
        # The tool properties panel gets rebuilt (clear_layout + deleteLater)
        # every time the user selects a different tool. hasattr() only
        # checks the Python attribute still exists, not that the underlying
        # C++ widget is still alive, so touching a stale llm_resp_box raises
        # a RuntimeError that - uncaught - would escalate to a hard process
        # abort instead of a catchable traceback.
        try:
            if hasattr(self, 'llm_resp_box'): self.llm_resp_box.setPlainText(text)
        except RuntimeError:
            pass

    def handle_llm_result(self, tool_item, response, generation):
        # LLMWorker runs on a background thread, so this fires as a queued,
        # cross-thread signal - potentially long after it was triggered, now
        # that local models can take 10-40s+ per query. If the watchdog
        # already gave up on this call (or a newer trigger superseded it),
        # this is a stale/abandoned worker finally reporting in - discard it
        # instead of clobbering whatever the current, newer state is.
        if generation != getattr(tool_item, 'llm_generation', 0):
            return
        tool_item.llm_is_processing = False
        tool_item.llm_response = response

        # Without an activation token the score just means "a reply arrived",
        # which is the long-standing behaviour and is left alone. With one, the
        # score reflects the *content* of the reply, so an LLM tool can gate a
        # sequence the way the pixel-based tools do.
        hit = evaluate_activation(response,
                                  getattr(tool_item, 'activation_token', ""),
                                  getattr(tool_item, 'activation_mode', "Off"))
        if hit is None:
            tool_item.current_score = 100
        else:
            tool_item.activation_hit = hit
            tool_item.current_score = 100 if hit else 0
            # LLM tools previously never touched tool_states, so they could not
            # drive a Studio Vision Wait at all. With a token they can.
            self.tool_states[tool_item.tool_name] = (
                tool_item.current_score >= tool_item.sensitivity)
            self.logger(
                f"LLM tool '{tool_item.tool_name}': "
                f"{'ACTIVATED' if hit else 'not activated'} "
                f"(looked for {tool_item.activation_token!r}, "
                f"mode '{tool_item.activation_mode}') -> score "
                f"{tool_item.current_score}")

        self.tool_responses[tool_item.tool_name] = response
        self.tool_response_times[tool_item.tool_name] = time.time()
        self._safe_set_llm_resp_box(tool_item.llm_response)
        # Data model first (above), widgets after - the panel may be long gone.
        self._refresh_activation_label(tool_item)
        try:
            if getattr(self, 'score_bar', None) is not None:
                self.score_bar.set_score(tool_item.current_score)
        except RuntimeError:
            pass

    def calculate_homography(self, tool_item):
        pts_src = np.array([[p.x() + tool_item.pos().x(), p.y() + tool_item.pos().y()] for p in tool_item.points],
                           dtype="float32")
        w = (tool_item.d1_2 + tool_item.d3_4) / 2.0
        h = (tool_item.d2_3 + tool_item.d4_1) / 2.0
        pts_dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype="float32")
        self.world_calibration_matrix = cv2.getPerspectiveTransform(pts_src, pts_dst)
        self.load_tool_properties_to_ui(tool_item)
        self.logger(f"Observatory: Calibrated World Matrix. Target assumed ~{w:.2f}x{h:.2f} in.")

    def apply_filters(self, item, base_gray_roi):
        processed = base_gray_roi.copy()
        if item.use_depth:
            k = item.filter_depth * 2 + 1
            processed = cv2.GaussianBlur(processed, (k, k), 0)
        if item.use_thresh:
            _, processed = cv2.threshold(processed, item.filter_thresh, 255, cv2.THRESH_BINARY)
        if item.use_edge:
            processed = cv2.Canny(processed, item.filter_edge, item.filter_edge * 2)
        return processed

    def reassign_tool_camera(self, tool_item, new_camera_index):
        if new_camera_index is None:
            return
        tool_item.camera_index = new_camera_index
        self.logger(f"Tool '{tool_item.tool_name}' reassigned to camera {new_camera_index}.")
        # Moving a tool to another camera immediately hides it if you're not
        # viewing that camera - deferred so this doesn't tear down the very
        # combo box whose signal is still executing.
        QTimer.singleShot(0, self.sync_tool_visibility_to_camera)

    def current_camera_index(self):
        """The camera currently selected for viewing (not necessarily running)."""
        data = self.camera_selector.currentData()
        return 0 if data is None else data

    def tool_belongs_to_current_camera(self, item):
        # Tools default to camera 0 if they predate per-tool camera binding,
        # so older project files keep working unchanged.
        return getattr(item, 'camera_index', 0) == self.current_camera_index()

    def sync_tool_visibility_to_camera(self):
        """Show only the tools bound to the camera being viewed.

        Hiding rather than deleting means every camera's tools stay live in
        the same project/file - switching the camera dropdown just changes
        which set you see and which set gets evaluated.
        """
        current = self.current_camera_index()
        for item in self.cam_scene.items():
            if not hasattr(item, 'tool_type'):
                continue
            item.setVisible(getattr(item, 'camera_index', 0) == current)
        # A tool that just got hidden shouldn't keep its properties panel up.
        selected = self.cam_scene.selectedItems()
        if selected and not selected[0].isVisible():
            self.cam_scene.clearSelection()
            self.build_empty_properties()
        self.refresh_tool_list()

    def refresh_tool_list(self):
        """Rebuild the left-hand tool list for the current camera only."""
        current = self.current_camera_index()
        self.active_tools_list.blockSignals(True)
        self.active_tools_list.clear()
        for item in self.cam_scene.items():
            if not hasattr(item, 'tool_type'):
                continue
            if getattr(item, 'camera_index', 0) != current:
                continue
            list_item = QListWidgetItem(item.tool_name)
            list_item.setFlags(list_item.flags() | Qt.ItemFlag.ItemIsEditable)
            list_item.setData(Qt.ItemDataRole.UserRole, item)
            self.active_tools_list.addItem(list_item)
        self.active_tools_list.blockSignals(False)

    def update_frame(self):
        # This runs on a 30ms QTimer. An uncaught exception escaping a
        # Qt-invoked slot gets escalated to a hard process abort by this
        # PyQt6 build instead of a catchable traceback, so every frame must
        # be exception-safe rather than letting a bad tool config or a
        # transient camera read glitch take the whole app down.
        try:
            self._update_frame_body()
        except Exception as e:
            self.logger(f"Frame processing error: {e}")

    def _update_frame_body(self):
        if self.camera and self.camera.isOpened():
            ret, frame = self.camera.read()
            if not ret: return
            self.current_raw_frame = frame.copy()
            display_frame = frame.copy()
            gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            # Auto-capture the reference frame once per camera start for any
            # Motion/Delete tool with it enabled, instead of requiring a
            # manual button press every time the camera gets bumped.
            for item in self.cam_scene.items():
                if (getattr(item, 'tool_type', None) in ("Motion Detection", "Delete (Missing Object)")
                        and self.tool_belongs_to_current_camera(item)
                        and getattr(item, 'auto_capture_reference', False)
                        and not getattr(item, 'has_auto_captured', False)):
                    self.set_motion_reference(item)
                    item.has_auto_captured = True
                    self.logger(f"Auto-captured reference state for '{item.tool_name}'.")

            active_item = self.cam_scene.selectedItems()[0] if self.cam_scene.selectedItems() else None

            for item in self.cam_scene.items():
                if not hasattr(item, 'tool_type'): continue
                # Tools bound to a different camera are neither drawn nor
                # evaluated against this camera's frame - their ROI
                # coordinates are meaningless here.
                if not self.tool_belongs_to_current_camera(item): continue

                if item.tool_type == "Reference Plane":
                    pts = np.array([[p.x() + item.pos().x(), p.y() + item.pos().y()] for p in item.points], np.int32)
                    cv2.polylines(display_frame, [pts], isClosed=True, color=(0, 165, 255), thickness=2)
                    continue

                x1, y1 = int(max(0, item.pos().x())), int(max(0, item.pos().y()))
                x2, y2 = int(min(frame.shape[1], x1 + item.rect().width())), int(
                    min(frame.shape[0], y1 + item.rect().height()))
                if x2 <= x1 or y2 <= y1: continue

                roi_gray = self.apply_filters(item, gray_frame[y1:y2, x1:x2])

                if item == active_item and item.tool_type != "LLM Vision":
                    display_frame[y1:y2, x1:x2] = cv2.cvtColor(roi_gray, cv2.COLOR_GRAY2BGR)

                if item.tool_type == "Image Capture":
                    # Not a detector - it only marks a region to grab. Draw
                    # the box so it's visible on the feed and move on without
                    # producing a trigger state.
                    cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 165, 255), 2)
                    cv2.putText(display_frame, item.tool_name, (x1, max(12, y1 - 6)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1)
                    continue

                if item.tool_type == "Pattern Match" and item.trained_template is not None:
                    h, w = item.trained_template.shape
                    if roi_gray.shape[0] >= h and roi_gray.shape[1] >= w:
                        res = cv2.matchTemplate(roi_gray, item.trained_template, cv2.TM_CCOEFF_NORMED)
                        _, max_val, _, max_loc = cv2.minMaxLoc(res)
                        item.current_score = int(max_val * 100)
                        if item.current_score >= item.sensitivity:
                            self.tool_states[item.tool_name] = True
                            cv2.rectangle(display_frame, (x1 + max_loc[0], y1 + max_loc[1]),
                                          (x1 + max_loc[0] + w, y1 + max_loc[1] + h), (0, 255, 0), 3)
                        else:
                            self.tool_states[item.tool_name] = False

                elif item.tool_type in ["Motion Detection",
                                        "Delete (Missing Object)"] and item.reference_frame is not None:
                    if roi_gray.shape == item.reference_frame.shape:
                        delta = cv2.absdiff(item.reference_frame, roi_gray)
                        _, thresh = cv2.threshold(delta, item.threshold, 255, cv2.THRESH_BINARY)
                        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        valid = [c for c in contours if cv2.contourArea(c) > item.min_area]

                        motion_pct = int(
                            (sum(cv2.contourArea(c) for c in valid) / (thresh.shape[0] * thresh.shape[1])) * 100)
                        item.current_score = min(100, motion_pct * 5)

                        if item.tool_type == "Delete (Missing Object)":
                            if item.current_score >= item.sensitivity:
                                self.tool_states[item.tool_name] = True
                                cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 0, 255), 4)
                                cv2.putText(display_frame, "MISSING", (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                                            (0, 0, 255), 2)
                            else:
                                self.tool_states[item.tool_name] = False
                        else:
                            if item.current_score >= item.sensitivity:
                                self.tool_states[item.tool_name] = True
                                for c in valid:
                                    (mx, my, mw, mh) = cv2.boundingRect(c)
                                    cv2.rectangle(display_frame, (x1 + mx, y1 + my), (x1 + mx + mw, y1 + my + mh),
                                                  (255, 165, 0), 2)
                            else:
                                self.tool_states[item.tool_name] = False

                elif item.tool_type == "Blob Detection":
                    work_img = roi_gray if item.blob_color == 255 else cv2.bitwise_not(roi_gray)
                    _, thresh = cv2.threshold(work_img, 127, 255, cv2.THRESH_BINARY)
                    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

                    valid_blobs = [c for c in contours if item.threshold <= cv2.contourArea(c) <= item.blob_max_area]
                    item.current_score = min(100, len(valid_blobs) * 20)

                    if item.current_score >= item.sensitivity and valid_blobs:
                        self.tool_states[item.tool_name] = True
                        best_c = max(valid_blobs, key=cv2.contourArea)
                        M = cv2.moments(best_c)

                        if M["m00"] != 0:
                            cx = int(M["m10"] / M["m00"]);
                            cy = int(M["m01"] / M["m00"])
                            if item.origin_handle:
                                ox, oy = item.origin_handle.pos().x(), item.origin_handle.pos().y()
                                item.last_blob_x = int(cx - ox);
                                item.last_blob_y = int(oy - cy)

                            rect = cv2.minAreaRect(best_c);
                            item.last_blob_a = rect[2]
                            gx, gy = x1 + cx, y1 + cy

                            cv2.drawMarker(display_frame, (gx, gy), (0, 255, 0), cv2.MARKER_CROSS, 20, 2)
                            box = cv2.boxPoints(rect);
                            box = np.int32(box) + np.array([x1, y1])
                            cv2.drawContours(display_frame, [box], 0, (255, 0, 255), 2)

                            if item == active_item and hasattr(self, 'blob_coord_label'):
                                self.blob_coord_label.setText(
                                    f"X: {item.last_blob_x} px | Y: {item.last_blob_y} px\nAngle: {item.last_blob_a:.1f}°")
                    else:
                        self.tool_states[item.tool_name] = False

                if item == active_item: self.score_bar.set_score(item.current_score)

            rgb_image = cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb_image.shape
            self.video_frame_item.setPixmap(
                QPixmap.fromImage(QImage(rgb_image.data, w, h, ch * w, QImage.Format.Format_RGB888)))

    def populate_cameras(self):
        self.camera_selector.clear()
        for i in range(4):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened(): self.camera_selector.addItem(f"Camera {i} (DSHOW)", i); cap.release()
        if self.camera_selector.count() == 0: self.camera_selector.addItem("No Cameras Found", 0)

    def stop_camera_feed(self):
        """Release the capture device and blank the view.

        Safe to call when nothing is running. current_raw_frame is cleared
        too: leaving the last frame of the *previous* camera lying around
        means a capture taken right after a switch would silently save an
        image from the camera you just switched away from.
        """
        self.timer.stop()
        try:
            if self.camera is not None:
                self.camera.release()
        except Exception:
            pass
        self.camera = None
        self.current_raw_frame = None
        self.video_frame_item.setPixmap(QPixmap())
        self.btn_toggle_cam.setText("Start Camera Feed")

    def start_camera_feed(self):
        """Open whichever device the Camera Device combo currently selects."""
        self.camera_index = self.camera_selector.currentData()
        if self.camera_index is None:
            return False
        self.camera = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        if not self.camera.isOpened():
            # Report it: a camera that silently fails to open looks identical
            # to one that opened but sees black.
            self.logger(f"Could not open camera {self.camera_index}.")
            self.btn_toggle_cam.setText("Start Camera Feed")
            return False
        self.timer.start(30)
        self.btn_toggle_cam.setText("Stop Camera Feed")
        # Give every auto-capture tool a fresh chance to re-train on
        # this camera session, in case its position shifted since
        # last time.
        for item in self.cam_scene.items():
            if hasattr(item, 'has_auto_captured'):
                item.has_auto_captured = False
        return True

    def toggle_camera(self):
        if self.timer.isActive():
            self.stop_camera_feed()
        else:
            self.start_camera_feed()

    def on_camera_selection_changed(self):
        """Swap which camera's tools are shown AND move the live feed.

        Previously this only re-filtered tool visibility, so picking a
        different device left the old one streaming - the selection looked
        like it did nothing. Only restarts if a feed is actually running, so
        choosing a camera while stopped still just arms the selection.
        """
        self.sync_tool_visibility_to_camera()
        if self.timer.isActive():
            self.stop_camera_feed()
            if self.start_camera_feed():
                self.logger(f"Switched live feed to camera {self.camera_index}.")

    def train_pattern_tool(self, tool_item):
        if self.current_raw_frame is None or not tool_item.pattern_roi: return
        p_roi = tool_item.pattern_roi
        gx, gy = int(tool_item.pos().x() + p_roi.pos().x()), int(tool_item.pos().y() + p_roi.pos().y())
        w, h = int(p_roi.rect().width()), int(p_roi.rect().height())
        x1, y1 = max(0, gx), max(0, gy)
        x2, y2 = min(self.current_raw_frame.shape[1], x1 + w), min(self.current_raw_frame.shape[0], y1 + h)
        if x2 > x1 and y2 > y1:
            train_crop = self.current_raw_frame[y1:y2, x1:x2]
            tool_item.raw_trained_template = cv2.cvtColor(train_crop, cv2.COLOR_BGR2GRAY)
            self.reapply_static_filters(tool_item)

    def set_motion_reference(self, tool_item):
        if self.current_raw_frame is None: return
        x1, y1 = int(max(0, tool_item.pos().x())), int(max(0, tool_item.pos().y()))
        x2, y2 = int(min(self.current_raw_frame.shape[1], x1 + tool_item.rect().width())), int(
            min(self.current_raw_frame.shape[0], y1 + tool_item.rect().height()))
        if x2 > x1 and y2 > y1:
            gray = cv2.cvtColor(self.current_raw_frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
            tool_item.raw_reference_frame = gray.copy()
            self.reapply_static_filters(tool_item)
            self.load_tool_properties_to_ui(tool_item)

    def spawn_tool_roi(self, x, y):
        selected_tool = self.tool_selector.currentText()
        self.tool_count += 1
        t_name = f"tool{self.tool_count:02d}_{selected_tool[:4]}"

        if selected_tool == "Reference Plane":
            roi = PerspectivePlaneROI(x - 100, y - 100, tool_name=t_name)
        else:
            roi = SearchROI(x - 125, y - 125, tool_name=t_name, tool_type=selected_tool)

        # A tool is born on whichever camera you're currently looking at.
        roi.camera_index = self.current_camera_index()

        self.cam_scene.addItem(roi)
        list_item = QListWidgetItem(t_name)
        list_item.setFlags(list_item.flags() | Qt.ItemFlag.ItemIsEditable)
        list_item.setData(Qt.ItemDataRole.UserRole, roi)
        self.active_tools_list.blockSignals(True)
        self.active_tools_list.addItem(list_item)
        self.active_tools_list.blockSignals(False)
        self.tool_states[t_name] = False
        self.tool_responses[t_name] = ""
        self.tool_response_times[t_name] = 0

    def sync_scene_to_list(self):
        selected = self.cam_scene.selectedItems()
        if not selected:
            self.active_tools_list.clearSelection()
            self.build_empty_properties()
            return
        item = selected[0]
        if hasattr(item, 'tool_name'):
            for i in range(self.active_tools_list.count()):
                l_item = self.active_tools_list.item(i)
                if l_item.data(Qt.ItemDataRole.UserRole) == item:
                    self.active_tools_list.blockSignals(True)
                    l_item.setSelected(True)
                    self.active_tools_list.blockSignals(False)
                    self.load_tool_properties_to_ui(item)
                    break

    def sync_list_to_scene(self):
        selected = self.active_tools_list.selectedItems()
        if not selected: return
        roi = selected[0].data(Qt.ItemDataRole.UserRole)
        if roi:
            self.cam_scene.blockSignals(True)
            self.cam_scene.clearSelection()
            roi.setSelected(True)
            self.cam_scene.blockSignals(False)
            self.load_tool_properties_to_ui(roi)

    def handle_tool_rename(self, item):
        roi = item.data(Qt.ItemDataRole.UserRole)
        if roi:
            old_name, new_name = roi.tool_name, item.text().strip()
            if new_name and old_name != new_name:
                roi.tool_name = new_name
                roi.label.setPlainText(f"{new_name}\n[{roi.tool_type}]")
                if old_name in self.tool_states: self.tool_states[new_name] = self.tool_states.pop(old_name)
                if old_name in self.tool_responses: self.tool_responses[new_name] = self.tool_responses.pop(old_name)
                if old_name in self.tool_response_times:
                    self.tool_response_times[new_name] = self.tool_response_times.pop(old_name)

    def delete_selected_tool(self):
        curr_item = self.active_tools_list.currentItem()
        if curr_item:
            roi = curr_item.data(Qt.ItemDataRole.UserRole)
            if roi in self.cam_scene.items(): self.cam_scene.removeItem(roi)
            if roi.tool_name in self.tool_states: del self.tool_states[roi.tool_name]
            if roi.tool_name in self.tool_responses: del self.tool_responses[roi.tool_name]
            if roi.tool_name in self.tool_response_times: del self.tool_response_times[roi.tool_name]
            self.active_tools_list.takeItem(self.active_tools_list.row(curr_item))
            self.build_empty_properties()

    def _pause_engine_timers(self):
        camera_was_active = self.timer.isActive()
        if camera_was_active:
            self.timer.stop()
        self.state_writer_timer.stop()
        self.command_listener_timer.stop()
        return camera_was_active

    def _resume_engine_timers(self, camera_was_active):
        if camera_was_active:
            self.timer.start(30)
        self.state_writer_timer.start(100)
        self.command_listener_timer.start(500)

    def prompt_open_file(self, title, filter_str):
        # Construct QFileDialog explicitly and force the option directly on the
        # instance: the static getOpenFileName()/getSaveFileName() convenience
        # methods can silently ignore options=DontUseNativeDialog on Windows,
        # which left the native (COM-based) dialog in play despite passing it.
        #
        # Also stop our own timers for the dialog's lifetime: the camera feed
        # and IPC timers fire every 30-500ms and touch scene/Qt objects, while
        # QFileDialog spins up its own background threads for directory
        # scanning. Running both concurrently is a plausible trigger for the
        # STATUS_STACK_BUFFER_OVERRUN crash seen in Qt6Core.dll.
        camera_was_active = self._pause_engine_timers()
        try:
            dialog = QFileDialog(self, title, "", filter_str)
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                files = dialog.selectedFiles()
                if files:
                    return files[0]
            return None
        finally:
            self._resume_engine_timers(camera_was_active)

    def prompt_save_file(self, title, filter_str):
        camera_was_active = self._pause_engine_timers()
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
            self._resume_engine_timers(camera_was_active)

    def save_project(self):
        file_path = self.prompt_save_file("Save Observatory Project", "JSON Files (*.json)")
        if not file_path: return

        project_data = {
            "camera_index": self.camera_selector.currentData(),
            "tools": [],
            "world_matrix_b64": base64.b64encode(self.world_calibration_matrix.tobytes()).decode(
                'utf-8') if self.world_calibration_matrix is not None else None
        }

        for item in self.cam_scene.items():
            if hasattr(item, 'tool_type'):
                if item.tool_type == "Reference Plane":
                    t_data = {
                        "name": item.tool_name, "type": item.tool_type, "x": item.pos().x(), "y": item.pos().y(),
                        "d1_2": item.d1_2, "d2_3": item.d2_3, "d3_4": item.d3_4, "d4_1": item.d4_1,
                        "points": [{"x": p.x(), "y": p.y()} for p in item.points]
                    }
                else:
                    t_data = {
                        "name": item.tool_name, "type": item.tool_type, "x": item.pos().x(), "y": item.pos().y(),
                        "w": item.rect().width(), "h": item.rect().height(), "sensitivity": item.sensitivity,
                        "threshold": item.threshold, "min_area": item.min_area,
                        "use_thresh": item.use_thresh, "filter_thresh": item.filter_thresh,
                        "use_edge": item.use_edge, "filter_edge": item.filter_edge,
                        "use_depth": item.use_depth, "filter_depth": item.filter_depth,
                        "blob_color": item.blob_color, "blob_max_area": item.blob_max_area,
                        "auto_capture_reference": item.auto_capture_reference,
                        "capture_mode": getattr(item, "capture_mode", "ROI"),
                        "captured_path": getattr(item, "captured_path", ""),
                        "cutout_path": getattr(item, "cutout_path", ""),
                        # llm_prompt was never saved: a configured prompt
                        # silently reverted to the default on reload, which
                        # makes a token-scored LLM tool useless.
                        "llm_prompt": getattr(item, "llm_prompt", ""),
                        "activation_mode": getattr(item, "activation_mode", "Off"),
                        "activation_token": getattr(item, "activation_token", "$ACTIVE"),
                        "raw_ref_frame_b64": img_to_b64(item.raw_reference_frame),
                        "raw_template_b64": img_to_b64(item.raw_trained_template)
                    }
                    if item.pattern_roi: t_data["pattern"] = {"x": item.pattern_roi.pos().x(),
                                                              "y": item.pattern_roi.pos().y(),
                                                              "w": item.pattern_roi.rect().width(),
                                                              "h": item.pattern_roi.rect().height()}
                    if item.origin_handle: t_data["origin"] = {"x": item.origin_handle.pos().x(),
                                                               "y": item.origin_handle.pos().y()}
                # Applies to both tool shapes, so it's set once here rather
                # than duplicated into each branch above.
                t_data["camera_index"] = getattr(item, 'camera_index', 0)
                project_data["tools"].append(t_data)

        with open(file_path, 'w') as f:
            json.dump(project_data, f)
        self.logger(f"Project saved successfully to {os.path.basename(file_path)}")

    def load_project(self, file_path=None):
        if not file_path:
            file_path = self.prompt_open_file("Open Observatory Project", "JSON Files (*.json)")
            if not file_path: return

        try:
            with open(file_path, 'r') as f:
                data = json.load(f)

            if not isinstance(data, dict) or "tools" not in data:
                # A Studio sequence file (authtest*.json/obstest*.json saved
                # via Studio's "Save Sequence") is a JSON list of StepBlocks,
                # not an Observatory project dict - loading one here used to
                # fail silently with a cryptic 'list' object has no attribute
                # 'get', leaving zero tools loaded and no clear signal why.
                self.logger(
                    f"Error loading project: '{os.path.basename(file_path)}' is not an Observatory "
                    f"project file (it looks like a Studio sequence file). Save an Observatory "
                    f"project from this app's own Save Project button instead.")
                return

            if self.timer.isActive(): self.toggle_camera()
            while self.active_tools_list.count() > 0:
                self.active_tools_list.setCurrentRow(0)
                self.delete_selected_tool()

            idx = self.camera_selector.findData(data.get("camera_index", 0))
            if idx >= 0: self.camera_selector.setCurrentIndex(idx)

            # Note: We do not start the camera automatically if loaded manually via UI,
            # only if triggered via IPC commands.

            if data.get("world_matrix_b64"):
                mat_bytes = base64.b64decode(data["world_matrix_b64"])
                self.world_calibration_matrix = np.frombuffer(mat_bytes, dtype=np.float32).reshape(3, 3)
            else:
                self.world_calibration_matrix = None

            for t in data.get("tools", []):
                if t["type"] == "Reference Plane":
                    roi = PerspectivePlaneROI(t["x"], t["y"], tool_name=t["name"])
                    roi.d1_2 = t.get("d1_2", 12.0);
                    roi.d2_3 = t.get("d2_3", 12.0);
                    roi.d3_4 = t.get("d3_4", 12.0);
                    roi.d4_1 = t.get("d4_1", 12.0)
                    if "points" in t:
                        roi.points = [QPointF(p["x"], p["y"]) for p in t["points"]]
                        roi.update_visual_polygon()
                        roi.reposition_handles()
                else:
                    roi = SearchROI(t["x"], t["y"], tool_name=t["name"], tool_type=t["type"])
                    roi.update_size(t["w"], t["h"])
                    roi.sensitivity = t.get("sensitivity", 50)
                    # Fall back to the constructor's per-tool-type default
                    # (see SearchROI) rather than a hardcoded 127, so a
                    # project saved before "threshold" was stored still gets
                    # a workable motion cutoff instead of a dead one.
                    roi.threshold = t.get("threshold", roi.threshold)
                    roi.min_area = t.get("min_area", 100)

                    roi.use_thresh = t.get("use_thresh", False)
                    roi.filter_thresh = t.get("filter_thresh", 127)
                    roi.use_edge = t.get("use_edge", False)
                    roi.filter_edge = t.get("filter_edge", 100)
                    roi.use_depth = t.get("use_depth", False)
                    roi.filter_depth = t.get("filter_depth", 5)

                    roi.blob_color = t.get("blob_color", 0)
                    roi.blob_max_area = t.get("blob_max_area", 5000)
                    roi.auto_capture_reference = t.get("auto_capture_reference", False)

                    # Fall back to the constructor defaults so projects saved
                    # before these existed still load.
                    roi.llm_prompt = t.get("llm_prompt", roi.llm_prompt)
                    roi.activation_mode = t.get("activation_mode", roi.activation_mode)
                    roi.activation_token = t.get("activation_token", roi.activation_token)
                    roi.capture_mode = t.get("capture_mode", "ROI")
                    roi.captured_path = t.get("captured_path", "")
                    roi.cutout_path = t.get("cutout_path", "")

                    roi.raw_reference_frame = b64_to_img(t.get("raw_ref_frame_b64", None))
                    roi.raw_trained_template = b64_to_img(t.get("raw_template_b64", None))
                    self.reapply_static_filters(roi)

                    if roi.pattern_roi and "pattern" in t:
                        roi.pattern_roi.setPos(t["pattern"]["x"], t["pattern"]["y"])
                        roi.pattern_roi.update_size(t["pattern"]["w"], t["pattern"]["h"])

                    if roi.origin_handle and "origin" in t:
                        roi.origin_handle.setPos(t["origin"]["x"], t["origin"]["y"])

                # Defaults to 0 so projects saved before per-tool camera
                # binding load unchanged, all on camera 0.
                roi.camera_index = t.get("camera_index", 0)

                self.cam_scene.addItem(roi)
                self.tool_states[roi.tool_name] = False
                self.tool_responses[roi.tool_name] = ""
                self.tool_response_times[roi.tool_name] = 0
                self.tool_count += 1

            # The list is rebuilt from scratch for the active camera rather
            # than appended to per-tool, so it never shows other cameras'
            # tools.
            self.sync_tool_visibility_to_camera()
            self.logger(f"Project loaded successfully from {os.path.basename(file_path)}")
        except Exception as e:
            self.logger(f"Error loading project: {e}")


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_THEME)
    observatory = ObservatoryEngine(logger=print)
    observatory.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()