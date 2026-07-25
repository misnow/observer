import os
import cv2
import numpy as np
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *

# Silence OpenCV Probing
os.environ["OPENCV_LOG_LEVEL"] = "FATAL"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"


class ObservatoryEngine(QMainWindow):
    def __init__(self, logger):
        super().__init__()
        self.logger = logger
        self.tool_states = {"None": True}
        self.camera = None
        self.timer = QTimer();
        self.timer.timeout.connect(self.update_frame)
        self.tool_count = 0
        self.setup_ui()

    def setup_ui(self):
        self.setWindowTitle("Observatory - Vision Engine")
        self.setGeometry(150, 150, 1200, 700)

        main_widget = QWidget();
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        # Camera Controls
        toolbar = QHBoxLayout()
        self.btn_toggle = QPushButton("Start Camera")
        self.btn_toggle.clicked.connect(self.toggle_camera)
        toolbar.addWidget(self.btn_toggle)
        layout.addLayout(toolbar)

        # Viewport
        self.cam_scene = QGraphicsScene()
        self.view = QGraphicsView(self.cam_scene)
        self.video_frame_item = self.cam_scene.addPixmap(QPixmap())
        layout.addWidget(self.view)

        # List of Tools
        self.tool_list = QListWidget()
        layout.addWidget(self.tool_list)

    def toggle_camera(self):
        if not self.camera or not self.camera.isOpened():
            self.camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if self.camera.isOpened():
                self.timer.start(30)
                self.btn_toggle.setText("Stop Camera")
        else:
            self.timer.stop()
            self.camera.release()
            self.btn_toggle.setText("Start Camera")

    def update_frame(self):
        if not self.camera or not self.camera.isOpened(): return
        ret, frame = self.camera.read()
        if not ret: return

        # --- VISION LOGIC ---
        # Draw all ROIs
        for item in self.cam_scene.items():
            if isinstance(item, QGraphicsRectItem) and hasattr(item, 'tool_type'):
                x, y = int(item.pos().x()), int(item.pos().y())
                w, h = int(item.rect().width()), int(item.rect().height())
                # Add tool processing here (Pattern match, motion, etc)
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        img = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        self.video_frame_item.setPixmap(QPixmap.fromImage(img))

    def closeEvent(self, event):
        if self.camera: self.camera.release()
        event.accept()

# Also include the classes for ProjectorCanvas, InteractiveTextItem,
# InteractiveMediaItem, FlowControlData, ConfirmationData in this file.