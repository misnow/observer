import sys
import os
import time

# ---------------------------------------------------------
# CRITICAL: Silence OpenCV C++ probing errors on Windows
# These must be set BEFORE importing cv2
# ---------------------------------------------------------
os.environ["OPENCV_LOG_LEVEL"] = "FATAL"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"

import cv2
import numpy as np
import xml.etree.ElementTree as ET
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QGraphicsView, QGraphicsScene,
                             QLabel, QSplitter, QComboBox, QToolBar, QStatusBar,
                             QMenuBar, QTableWidget, QTableWidgetItem, QTextBrowser,
                             QHeaderView, QFileDialog)
from PyQt6.QtCore import pyqtSignal, QTimer, Qt
from PyQt6.QtGui import QColor, QBrush, QImage, QPixmap, QAction

# Professional Dark Theme Stylesheet
DARK_THEME = """
QWidget { background-color: #2b2b2b; color: #a9b7c6; font-family: 'Segoe UI', Arial; }
QPushButton { background-color: #4C5052; border: 1px solid #5C5C42; padding: 5px 15px; border-radius: 3px; color: #ffffff; }
QPushButton:hover { background-color: #5C6062; }
QPushButton:pressed { background-color: #3C3F41; }
QTableWidget { background-color: #313335; gridline-color: #555555; border: 1px solid #1e1e1e; }
QHeaderView::section { background-color: #3C3F41; padding: 4px; border: 1px solid #1e1e1e; }
QTextBrowser { background-color: #1e1e1e; color: #a9b7c6; border: 1px solid #555555; font-family: 'Consolas', monospace; }
QComboBox { background-color: #3C3F41; border: 1px solid #1e1e1e; padding: 2px; }
QMenuBar { background-color: #3C3F41; border-bottom: 1px solid #1e1e1e; }
QMenuBar::item:selected { background-color: #5C6062; }
"""


# ---------------------------------------------------------
# 1. THE PROJECTOR CANVAS
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

        self.view = QGraphicsView(self.scene)
        self.view.setStyleSheet("border: none; background-color: black;")
        self.setCentralWidget(self.view)

    def clear_canvas(self):
        self.scene.clear()


# ---------------------------------------------------------
# 2. THE OBSERVATORY ENGINE (Vision)
# ---------------------------------------------------------
class ObservatoryEngine(QMainWindow):
    trigger_signal = pyqtSignal(str, bool)

    def __init__(self, logger):
        super().__init__()
        self.logger = logger
        self.setWindowTitle("Observatory - Vision Environment")
        self.setGeometry(100, 100, 800, 600)

        self.camera = None
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)

        self.video_label = QLabel("No Camera Selected")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet("background-color: #1e1e1e; border: 1px solid #555;")
        self.layout.addWidget(self.video_label)

        self.setup_toolbar()

    def setup_toolbar(self):
        toolbar = QToolBar("Camera Tools")
        self.addToolBar(toolbar)

        toolbar.addWidget(QLabel(" Devices: "))
        self.camera_selector = QComboBox()
        self.populate_cameras()
        toolbar.addWidget(self.camera_selector)

        self.btn_toggle_cam = QPushButton("Start Camera")
        self.btn_toggle_cam.clicked.connect(self.toggle_camera)
        toolbar.addWidget(self.btn_toggle_cam)

    def populate_cameras(self):
        self.camera_selector.clear()
        # Probing empty indices will no longer print C++ errors to the console
        for i in range(3):
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                self.camera_selector.addItem(f"Camera {i}", i)
                cap.release()

        if self.camera_selector.count() == 0:
            self.camera_selector.addItem("No Cameras Found")

    def toggle_camera(self):
        if self.timer.isActive():
            self.timer.stop()
            if self.camera:
                self.camera.release()
            self.video_label.setText("Camera Stopped")
            self.btn_toggle_cam.setText("Start Camera")
            self.logger("Camera stream stopped.")
        else:
            cam_idx = self.camera_selector.currentData()
            if cam_idx is not None:
                self.camera = cv2.VideoCapture(cam_idx)

                # FORCE RESOLUTION TO PREVENT CROPPING/ZOOMING
                self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

                if self.camera.isOpened():
                    self.timer.start(30)
                    self.btn_toggle_cam.setText("Stop Camera")
                    self.logger(
                        f"Camera {cam_idx} started at {self.camera.get(cv2.CAP_PROP_FRAME_WIDTH)}x{self.camera.get(cv2.CAP_PROP_FRAME_HEIGHT)}.")
                else:
                    self.logger(f"ERROR: Failed to open Camera {cam_idx}.", is_error=True)

    def update_frame(self):
        if self.camera and self.camera.isOpened():
            ret, frame = self.camera.read()
            if ret:
                rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb_image.shape
                bytes_per_line = ch * w
                qt_image = QImage(rgb_image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)

                # SMOOTH TRANSFORMATION PREVENTS PIXELATION/UGLY SCALING
                pixmap = QPixmap.fromImage(qt_image).scaled(
                    self.video_label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
                self.video_label.setPixmap(pixmap)

    def closeEvent(self, event):
        if self.camera and self.camera.isOpened():
            self.camera.release()
        event.accept()


# ---------------------------------------------------------
# 3. THE AUTHORING & RUN INTERFACE (Main IDE)
# ---------------------------------------------------------
class AuthoringInterface(QMainWindow):
    def __init__(self, canvas):
        super().__init__()
        self.setWindowTitle("LightGuide Studio - Authoring Environment")
        self.setGeometry(200, 200, 1200, 800)

        self.canvas = canvas
        self.current_file = None

        self.setup_ui()
        self.observatory = ObservatoryEngine(self.log_message)
        self.log_message("System Initialized.")

    def setup_ui(self):
        # 1. Menu Bar
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")

        action_new = QAction("New Design", self)
        action_new.triggered.connect(self.new_file)
        file_menu.addAction(action_new)

        action_open = QAction("Open XML...", self)
        action_open.triggered.connect(self.open_file)
        file_menu.addAction(action_open)

        action_save = QAction("Save", self)
        action_save.triggered.connect(self.save_file)
        file_menu.addAction(action_save)

        file_menu.addSeparator()

        action_exit = QAction("Exit", self)
        action_exit.triggered.connect(self.close)
        file_menu.addAction(action_exit)

        # 2. Main Layout containing Splitters
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Tool Bar (System Options)
        toolbar = QHBoxLayout()
        self.btn_design_mode = QPushButton("✏️ Design Mode")
        self.btn_run_mode = QPushButton("▶️ Run Mode")
        self.btn_clear_canvas = QPushButton("🗑️ Clear Canvas")
        self.btn_show_obs = QPushButton("👁️ Launch Observatory")

        self.btn_show_obs.clicked.connect(lambda: self.observatory.show())
        self.btn_clear_canvas.clicked.connect(self.canvas.clear_canvas)

        toolbar.addWidget(self.btn_design_mode)
        toolbar.addWidget(self.btn_run_mode)
        toolbar.addWidget(self.btn_clear_canvas)
        toolbar.addStretch()
        toolbar.addWidget(self.btn_show_obs)
        main_layout.addLayout(toolbar)

        # Vertical Splitter (Top: Work Area, Bottom: Console)
        v_splitter = QSplitter(Qt.Orientation.Vertical)

        # Horizontal Splitter (Left: Tools, Right: Steps Timeline)
        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left Panel (Asset Toolbox)
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("<b>Asset Toolbox</b>"))

        self.btn_insert_text = QPushButton("Insert Text")
        self.btn_insert_img = QPushButton("Insert Image")
        self.btn_insert_vid = QPushButton("Insert Video")

        left_layout.addWidget(self.btn_insert_text)
        left_layout.addWidget(self.btn_insert_img)
        left_layout.addWidget(self.btn_insert_vid)
        left_layout.addStretch()

        # Right Panel (Execution Steps)
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("<b>Execution Timeline (Steps)</b>"))

        self.step_table = QTableWidget(0, 4)
        self.step_table.setHorizontalHeaderLabels(["Step", "Asset Type", "Trigger Source", "Properties"])
        self.step_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.step_table.verticalHeader().setVisible(False)
        right_layout.addWidget(self.step_table)

        h_splitter.addWidget(left_panel)
        h_splitter.addWidget(right_panel)
        h_splitter.setSizes([250, 950])

        # Bottom Console (Logging Window)
        self.console = QTextBrowser()
        self.console.setReadOnly(True)

        v_splitter.addWidget(h_splitter)
        v_splitter.addWidget(self.console)
        v_splitter.setSizes([650, 150])  # 80% work area, 20% console

        main_layout.addWidget(v_splitter)

    # --- File Operations ---
    def new_file(self):
        self.step_table.setRowCount(0)
        self.current_file = None
        self.canvas.clear_canvas()
        self.log_message("Started new design sequence.")

    def open_file(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Open Authoring File", "", "XML Files (*.xml)")
        if filepath:
            self.current_file = filepath
            self.log_message(f"Loaded configuration from: {filepath}")

    def save_file(self):
        if not self.current_file:
            filepath, _ = QFileDialog.getSaveFileName(self, "Save Authoring File", "", "XML Files (*.xml)")
            if filepath:
                self.current_file = filepath

        if self.current_file:
            self.log_message(f"Saved configuration to: {self.current_file}")

    # --- UI Logic ---
    def add_step(self, step_num, asset_type, trigger, properties):
        row = self.step_table.rowCount()
        self.step_table.insertRow(row)
        self.step_table.setItem(row, 0, QTableWidgetItem(str(step_num)))
        self.step_table.setItem(row, 1, QTableWidgetItem(asset_type))
        self.step_table.setItem(row, 2, QTableWidgetItem(trigger))
        self.step_table.setItem(row, 3, QTableWidgetItem(properties))

    def log_message(self, message, is_error=False):
        """Appends timestamped messages to the bottom console."""
        timestamp = time.strftime("[%H:%M:%S]")
        color = "red" if is_error else "#00FF00"
        formatted_message = f"<span style='color:{color};'>{timestamp} {message}</span>"
        self.console.append(formatted_message)


# ---------------------------------------------------------
# 4. ORCHESTRATOR
# ---------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_THEME)

    canvas = ProjectorCanvas()
    authoring = AuthoringInterface(canvas)

    canvas.show()
    authoring.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()