import sys
import os
import json
import base64
import time

# Silence OpenCV C++ probing errors
os.environ["OPENCV_LOG_LEVEL"] = "FATAL"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"
os.environ["OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"] = "0"

import cv2
import numpy as np
from PyQt6.QtWidgets import *
from PyQt6.QtCore import pyqtSignal, QTimer, Qt, QPointF, QRect
from PyQt6.QtGui import QColor, QBrush, QImage, QPixmap, QFont, QPen, QPainter, QPolygonF, QAction

# Try to import MediaPipe for Human Rigging
try:
    import mediapipe as mp

    mp_pose = mp.solutions.pose
    mp_drawing = mp.solutions.drawing_utils
    MP_AVAILABLE = True
except Exception as e:
    MP_AVAILABLE = False

# Professional Dark Theme Stylesheet
DARK_THEME = """
QWidget { background-color: #2b2b2b; color: #a9b7c6; font-family: 'Segoe UI', Arial; }
QPushButton { background-color: #4C5052; border: 1px solid #5C5C42; padding: 5px 15px; border-radius: 3px; color: #ffffff; }
QPushButton:hover { background-color: #5C6062; }
QPushButton:pressed { background-color: #3C3F41; }
QPushButton:disabled { background-color: #1e1e1e; color: #555555; border: 1px solid #333333; }
QListWidget { background-color: #313335; border: 1px solid #1e1e1e; }
QTreeWidget { background-color: #1e1e1e; border: 1px solid #555; color: #a9b7c6; }
QHeaderView::section { background-color: #3C3F41; padding: 4px; border: 1px solid #1e1e1e; font-weight: bold; }
QComboBox, QSpinBox, QLineEdit { background-color: #3C3F41; border: 1px solid #1e1e1e; padding: 3px; color: #ffffff; }
QMenuBar { background-color: #3C3F41; border-bottom: 1px solid #1e1e1e; }
QMenuBar::item:selected { background-color: #2f65ca; }
QMenu { background-color: #313335; border: 1px solid #555; }
QMenu::item:selected { background-color: #2f65ca; }
QGroupBox { border: 1px solid #555555; margin-top: 10px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
QSlider::groove:horizontal { border: 1px solid #555; height: 8px; background: #3C3F41; margin: 2px 0; border-radius: 4px; }
QSlider::handle:horizontal { background: #4C5052; border: 1px solid #5C5C42; width: 14px; margin: -4px 0; border-radius: 7px; }
"""


# ---------------------------------------------------------
# IMAGE SERIALIZATION HELPERS
# ---------------------------------------------------------
def img_to_b64(img):
    if img is None: return None
    _, buffer = cv2.imencode('.png', img)
    return base64.b64encode(buffer).decode('utf-8')


def b64_to_img(b64_str):
    if not b64_str: return None
    img_data = base64.b64decode(b64_str)
    nparr = np.frombuffer(img_data, np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)


# ---------------------------------------------------------
# OBSERVATORY ENGINE (Vision Processing Workspace)
# ---------------------------------------------------------
class ROIResizeHandle(QGraphicsRectItem):
    def __init__(self, parent, color_string):
        super().__init__(0, 0, 12, 12, parent)
        self.setBrush(QBrush(QColor(color_string)))
        self.setPen(QPen(QColor("white"), 1))
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._dragging = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton: self._dragging = True; event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            pos_in_parent = self.parentItem().mapFromItem(self, event.pos())
            self.parentItem().update_size(max(30, pos_in_parent.x()), max(30, pos_in_parent.y()))
            event.accept()

    def mouseReleaseEvent(self, event):
        self._dragging = False;
        event.accept()


class PatternROI(QGraphicsRectItem):
    def __init__(self, parent_search_roi):
        super().__init__(0, 0, 100, 100, parent_search_roi)
        self.setPos(50, 50)
        self.setPen(QPen(QColor("red"), 2))
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.handle = ROIResizeHandle(self, "red")
        self.update_handle_pos()

    def update_size(self, w, h):
        if self.parentItem():
            pw, ph = self.parentItem().rect().width(), self.parentItem().rect().height()
            self.setRect(0, 0, max(20, min(w, pw - self.pos().x())), max(20, min(h, ph - self.pos().y())))
            self.update_handle_pos()

    def update_handle_pos(self):
        self.handle.setPos(self.rect().width() - 6, self.rect().height() - 6)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.parentItem():
            new_x = max(0, min(value.x(), self.parentItem().rect().width() - self.rect().width()))
            new_y = max(0, min(value.y(), self.parentItem().rect().height() - self.rect().height()))
            return QPointF(new_x, new_y)
        return super().itemChange(change, value)


class SearchROI(QGraphicsRectItem):
    def __init__(self, x, y, tool_name, tool_type):
        super().__init__(0, 0, 250, 250)
        self.tool_name = tool_name
        self.tool_type = tool_type

        # Core Properties
        self.threshold = 127
        self.sensitivity = 50
        self.min_area = 100  # For blob/motion

        # State tracking
        self.current_score = 0
        self.is_triggered = False
        self.reference_frame = None
        self.trained_template = None
        self.last_blob_x = 0
        self.last_blob_y = 0

        # Pre-Processing Filters
        self.use_thresh = False
        self.filter_thresh = 127
        self.use_edge = False
        self.filter_edge = 100
        self.use_depth = False  # Acts as Blur/Depth
        self.filter_depth = 5

        self.setPos(x, y)
        color_map = {"Delete (Missing Object)": "#e74c3c", "Motion Detection": "#3498db", "Blob Detection": "#9b59b6"}
        color = color_map.get(tool_type, "white")

        self.setPen(QPen(QColor(color), 2, Qt.PenStyle.DashLine))
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        self.handle = ROIResizeHandle(self, color)
        self.update_handle_pos()

        self.pattern_roi = PatternROI(self) if self.tool_type == "Pattern Match" else None

        self.label = QGraphicsTextItem(f"{tool_name}\n[{tool_type}]", self)
        self.label.setDefaultTextColor(QColor(color))
        self.label.setPos(5, 5)

    def update_size(self, w, h):
        self.setRect(0, 0, w, h)
        self.update_handle_pos()
        if self.pattern_roi:
            self.pattern_roi.setPos(min(self.pattern_roi.pos().x(), w - self.pattern_roi.rect().width()),
                                    min(self.pattern_roi.pos().y(), h - self.pattern_roi.rect().height()))

    def update_handle_pos(self):
        self.handle.setPos(self.rect().width() - 6, self.rect().height() - 6)


class VisionCanvasView(QGraphicsView):
    double_clicked = pyqtSignal(float, float)

    def __init__(self, scene): super().__init__(scene)

    def mouseDoubleClickEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        self.double_clicked.emit(scene_pos.x(), scene_pos.y())
        super().mouseDoubleClickEvent(event)


class ThresholdScoreBar(QWidget):
    trigger_state_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(40)
        self.score, self.threshold, self.is_triggered = 0, 50, False
        self._dragging = False

    def set_score(self, val):
        self.score = max(0, min(100, val))
        was_triggered = self.is_triggered
        self.is_triggered = (self.score >= self.threshold)
        if self.is_triggered != was_triggered: self.trigger_state_changed.emit(self.is_triggered)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor("#1a1a1a"))
        fill_width = int((self.score / 100.0) * rect.width())
        painter.fillRect(QRect(0, 0, fill_width, rect.height()),
                         QColor(0, 200, 0, 180) if self.score >= self.threshold else QColor(220, 0, 0, 180))
        t_x = int((self.threshold / 100.0) * rect.width())
        painter.setPen(QPen(QColor("white"), 3))
        painter.drawLine(t_x, 0, t_x, rect.height())
        painter.setPen(QColor("white"))
        painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                         f"Signal Score: {self.score}%  |  Trigger Line: {self.threshold}%")

    def mousePressEvent(self, event):
        self.update_threshold_from_mouse(event.pos().x()); self._dragging = True

    def mouseMoveEvent(self, event):
        if self._dragging: self.update_threshold_from_mouse(event.pos().x())

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def update_threshold_from_mouse(self, x):
        self.threshold = max(0, min(100, int((x / self.width()) * 100)))
        self.update()


class ObservatoryEngine(QMainWindow):
    def __init__(self, logger):
        super().__init__()
        self.logger = logger
        self.setWindowTitle("Observatory - Professional Vision Environment")
        self.setGeometry(150, 150, 1400, 850)

        self.camera = None
        self.camera_index = 0
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.tool_count = 0
        self.current_raw_frame = None

        self.tool_states = {"None": True}

        if MP_AVAILABLE:
            self.pose_tracker = mp_pose.Pose(min_detection_confidence=0.5, min_tracking_confidence=0.5)

        self.setup_ui()
        self.build_menubar()

    def build_menubar(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")

        open_action = QAction("📂 Open Project", self)
        open_action.triggered.connect(self.load_project)
        file_menu.addAction(open_action)

        save_action = QAction("💾 Save Project", self)
        save_action.triggered.connect(self.save_project)
        file_menu.addAction(save_action)

    def setup_ui(self):
        main_layout = QVBoxLayout()
        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Camera Device: "))
        self.camera_selector = QComboBox()
        self.populate_cameras()
        toolbar.addWidget(self.camera_selector)

        self.btn_toggle_cam = QPushButton("Start Camera Feed")
        self.btn_toggle_cam.clicked.connect(self.toggle_camera)
        toolbar.addWidget(self.btn_toggle_cam)
        toolbar.addStretch()
        main_layout.addLayout(toolbar)

        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left Panel
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(QLabel("<b>Vision Tool Arsenal</b>"))

        self.tool_selector = QComboBox()
        available_tools = ["Pattern Match", "Blob Detection", "Delete (Missing Object)", "Motion Detection"]
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

        # Center Panel
        self.cam_scene = QGraphicsScene()
        self.cam_scene.selectionChanged.connect(self.sync_scene_to_list)
        self.cam_view = VisionCanvasView(self.cam_scene)
        self.cam_view.setStyleSheet("background-color: #1e1e1e; border: 1px solid #555;")
        self.cam_view.double_clicked.connect(self.spawn_tool_roi)
        self.video_frame_item = self.cam_scene.addPixmap(QPixmap())
        h_splitter.addWidget(self.cam_view)

        # Right Panel
        right_panel = QWidget()
        self.right_layout = QVBoxLayout(right_panel)
        self.right_layout.addWidget(QLabel("<b>Tool Inspector</b>"))

        # Pre-Processing Filters Group
        self.filter_group = QGroupBox("Pre-Processing Filters")
        self.filter_layout = QFormLayout(self.filter_group)
        self.right_layout.addWidget(self.filter_group)

        # Tool Properties Group
        self.tool_prop_group = QGroupBox("Tool Settings & Output")
        self.tool_prop_layout = QFormLayout(self.tool_prop_group)
        self.right_layout.addWidget(self.tool_prop_group)

        # Trigger Group
        self.score_group = QGroupBox("Dynamic Trigger Output")
        score_layout = QVBoxLayout(self.score_group)
        self.score_bar = ThresholdScoreBar()
        self.score_bar.trigger_state_changed.connect(self.sync_threshold_to_tool)
        score_layout.addWidget(self.score_bar)
        self.right_layout.addWidget(self.score_group)

        self.right_layout.addStretch()
        h_splitter.addWidget(right_panel)

        h_splitter.setSizes([250, 800, 320])
        main_layout.addWidget(h_splitter)
        self.build_empty_properties()

    def build_empty_properties(self):
        self.clear_layout(self.filter_layout)
        self.clear_layout(self.tool_prop_layout)
        self.filter_group.setVisible(False)
        self.score_group.setVisible(False)
        self.tool_prop_layout.addRow(QLabel("<i>Select a tool to view properties.</i>"))

    def clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()

    def sync_threshold_to_tool(self, triggered):
        items = self.cam_scene.selectedItems()
        if items and hasattr(items[0], 'sensitivity'): items[0].sensitivity = self.score_bar.threshold

    def load_tool_properties_to_ui(self, tool_item):
        self.clear_layout(self.filter_layout)
        self.clear_layout(self.tool_prop_layout)
        self.filter_group.setVisible(True)
        self.score_group.setVisible(True)

        self.score_bar.blockSignals(True)
        self.score_bar.threshold = tool_item.sensitivity
        self.score_bar.set_score(tool_item.current_score)
        self.score_bar.blockSignals(False)

        # Build Filters UI
        chk_thresh = QCheckBox("Apply Thresholding")
        chk_thresh.setChecked(tool_item.use_thresh)
        chk_thresh.toggled.connect(lambda v: setattr(tool_item, 'use_thresh', v))
        sld_thresh = QSlider(Qt.Orientation.Horizontal)
        sld_thresh.setRange(0, 255);
        sld_thresh.setValue(tool_item.filter_thresh)
        sld_thresh.valueChanged.connect(lambda v: setattr(tool_item, 'filter_thresh', v))

        chk_depth = QCheckBox("Apply Depth/Blur")
        chk_depth.setChecked(tool_item.use_depth)
        chk_depth.toggled.connect(lambda v: setattr(tool_item, 'use_depth', v))
        sld_depth = QSlider(Qt.Orientation.Horizontal)
        sld_depth.setRange(1, 15);
        sld_depth.setValue(tool_item.filter_depth)
        sld_depth.valueChanged.connect(lambda v: setattr(tool_item, 'filter_depth', v))

        chk_edge = QCheckBox("Apply Edge Detection")
        chk_edge.setChecked(tool_item.use_edge)
        chk_edge.toggled.connect(lambda v: setattr(tool_item, 'use_edge', v))
        sld_edge = QSlider(Qt.Orientation.Horizontal)
        sld_edge.setRange(10, 200);
        sld_edge.setValue(tool_item.filter_edge)
        sld_edge.valueChanged.connect(lambda v: setattr(tool_item, 'filter_edge', v))

        self.filter_layout.addRow(chk_thresh, sld_thresh)
        self.filter_layout.addRow(chk_depth, sld_depth)
        self.filter_layout.addRow(chk_edge, sld_edge)

        # Build Tool Specifics
        type_label = QLabel(f"<b>{tool_item.tool_type}</b>")
        type_label.setStyleSheet("color: #3498db;")
        self.tool_prop_layout.addRow("Tool Logic:", type_label)

        if tool_item.tool_type == "Pattern Match":
            btn_train = QPushButton("🎯 Train Pattern")
            btn_train.clicked.connect(lambda: self.train_pattern_tool(tool_item))
            self.tool_prop_layout.addRow(btn_train)

            thumb = QLabel("No Trained Image")
            thumb.setFixedSize(100, 60)
            thumb.setStyleSheet("background-color: #1e1e1e; border: 1px dashed #555;")
            if tool_item.trained_template is not None:
                rgb_crop = cv2.cvtColor(tool_item.trained_template, cv2.COLOR_GRAY2RGB)
                h, w, ch = rgb_crop.shape
                img = QImage(rgb_crop.data, w, h, ch * w, QImage.Format.Format_RGB888)
                thumb.setPixmap(QPixmap.fromImage(img).scaled(100, 60, Qt.AspectRatioMode.KeepAspectRatio))
            self.tool_prop_layout.addRow("Template:", thumb)

        elif tool_item.tool_type in ["Motion Detection", "Delete (Missing Object)"]:
            slider_thresh = QSlider(Qt.Orientation.Horizontal)
            slider_thresh.setRange(1, 255);
            slider_thresh.setValue(tool_item.threshold)
            slider_thresh.valueChanged.connect(lambda v: setattr(tool_item, 'threshold', v))
            self.tool_prop_layout.addRow("Pixel Diff Thresh:", slider_thresh)

            slider_area = QSlider(Qt.Orientation.Horizontal)
            slider_area.setRange(10, 5000);
            slider_area.setValue(tool_item.min_area)
            slider_area.valueChanged.connect(lambda v: setattr(tool_item, 'min_area', v))
            self.tool_prop_layout.addRow("Min Motion Area:", slider_area)

            btn_ref = QPushButton("📷 Capture Reference State")
            btn_ref.setStyleSheet("background-color: #c0392b; font-weight: bold;")
            btn_ref.clicked.connect(lambda: self.set_motion_reference(tool_item))
            self.tool_prop_layout.addRow(btn_ref)

            status = QLabel("Ready" if tool_item.reference_frame is not None else "Missing Reference Frame")
            self.tool_prop_layout.addRow("Status:", status)

        elif tool_item.tool_type == "Blob Detection":
            slider_area = QSlider(Qt.Orientation.Horizontal)
            slider_area.setRange(10, 5000);
            slider_area.setValue(tool_item.threshold)
            slider_area.valueChanged.connect(lambda v: setattr(tool_item, 'threshold', v))
            self.tool_prop_layout.addRow("Min Blob Area:", slider_area)

            self.blob_coord_label = QLabel(f"X: {tool_item.last_blob_x} | Y: {tool_item.last_blob_y}")
            self.blob_coord_label.setStyleSheet("color: #2ecc71; font-weight: bold;")
            self.tool_prop_layout.addRow("Center (from BL):", self.blob_coord_label)

    def train_pattern_tool(self, tool_item):
        if self.current_raw_frame is None or not tool_item.pattern_roi: return
        p_roi = tool_item.pattern_roi
        gx, gy = int(tool_item.pos().x() + p_roi.pos().x()), int(tool_item.pos().y() + p_roi.pos().y())
        w, h = int(p_roi.rect().width()), int(p_roi.rect().height())
        x1, y1 = max(0, gx), max(0, gy)
        x2, y2 = min(self.current_raw_frame.shape[1], x1 + w), min(self.current_raw_frame.shape[0], y1 + h)
        if x2 > x1 and y2 > y1:
            train_crop = self.current_raw_frame[y1:y2, x1:x2]
            tool_item.trained_template = cv2.cvtColor(train_crop, cv2.COLOR_BGR2GRAY)
            self.load_tool_properties_to_ui(tool_item)

    def set_motion_reference(self, tool_item):
        if self.current_raw_frame is None: return
        x1, y1 = int(max(0, tool_item.pos().x())), int(max(0, tool_item.pos().y()))
        x2, y2 = int(min(self.current_raw_frame.shape[1], x1 + tool_item.rect().width())), int(
            min(self.current_raw_frame.shape[0], y1 + tool_item.rect().height()))
        if x2 > x1 and y2 > y1:
            gray = cv2.cvtColor(self.current_raw_frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
            # Apply depth blur if configured
            if tool_item.use_depth:
                k = tool_item.filter_depth * 2 + 1
                gray = cv2.GaussianBlur(gray, (k, k), 0)
            tool_item.reference_frame = gray
            self.load_tool_properties_to_ui(tool_item)

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

    def update_frame(self):
        if self.camera and self.camera.isOpened():
            ret, frame = self.camera.read()
            if not ret: return
            self.current_raw_frame = frame.copy()
            display_frame = frame.copy()
            gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            frame_height = frame.shape[0]

            active_item = self.cam_scene.selectedItems()[0] if self.cam_scene.selectedItems() else None

            for item in self.cam_scene.items():
                if not hasattr(item, 'tool_type'): continue

                x1, y1 = int(max(0, item.pos().x())), int(max(0, item.pos().y()))
                x2, y2 = int(min(frame.shape[1], x1 + item.rect().width())), int(
                    min(frame.shape[0], y1 + item.rect().height()))
                if x2 <= x1 or y2 <= y1: continue

                # Apply User Filters First
                roi_gray = self.apply_filters(item, gray_frame[y1:y2, x1:x2])

                # Pattern Match Logic
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

                # Motion & Delete Logic (Frame Subtraction)
                elif item.tool_type in ["Motion Detection",
                                        "Delete (Missing Object)"] and item.reference_frame is not None:
                    if roi_gray.shape == item.reference_frame.shape:
                        delta = cv2.absdiff(item.reference_frame, roi_gray)
                        _, thresh = cv2.threshold(delta, item.threshold, 255, cv2.THRESH_BINARY)

                        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        valid_motion = [c for c in contours if cv2.contourArea(c) > item.min_area]

                        motion_pct = int(
                            (sum(cv2.contourArea(c) for c in valid_motion) / (thresh.shape[0] * thresh.shape[1])) * 100)
                        item.current_score = min(100, motion_pct * 5)  # Amplify score for UI

                        # Logic flip for Delete vs Motion
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
                                for c in valid_motion:
                                    (mx, my, mw, mh) = cv2.boundingRect(c)
                                    cv2.rectangle(display_frame, (x1 + mx, y1 + my), (x1 + mx + mw, y1 + my + mh),
                                                  (255, 165, 0), 2)
                            else:
                                self.tool_states[item.tool_name] = False

                # Blob Logic
                elif item.tool_type == "Blob Detection":
                    # Ensure image is inverted for blob detection (dark blobs on light bg)
                    blob_target = cv2.bitwise_not(roi_gray)
                    params = cv2.SimpleBlobDetector_Params()
                    params.filterByArea = True
                    params.minArea = item.threshold
                    detector = cv2.SimpleBlobDetector_create(params)
                    keypoints = detector.detect(blob_target)

                    item.current_score = min(100, len(keypoints) * 20)

                    if item.current_score >= item.sensitivity and keypoints:
                        self.tool_states[item.tool_name] = True
                        best_kp = max(keypoints, key=lambda k: k.size)

                        # Calculate True Bottom-Left Coords
                        bx = int(x1 + best_kp.pt[0])
                        by = int(frame_height - (y1 + best_kp.pt[1]))
                        item.last_blob_x, item.last_blob_y = bx, by

                        # Draw Crosshair
                        cx, cy = int(x1 + best_kp.pt[0]), int(y1 + best_kp.pt[1])
                        r = int(best_kp.size / 2)
                        cv2.circle(display_frame, (cx, cy), r, (255, 0, 255), 2)
                        cv2.drawMarker(display_frame, (cx, cy), (0, 255, 0), cv2.MARKER_CROSS, 20, 2)

                        if item == active_item and hasattr(self, 'blob_coord_label'):
                            self.blob_coord_label.setText(f"X: {bx} | Y: {by}")
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
            if cap.isOpened():
                self.camera_selector.addItem(f"Camera {i} (DSHOW)", i)
                cap.release()
        if self.camera_selector.count() == 0: self.camera_selector.addItem("No Cameras Found", 0)

    def toggle_camera(self):
        if self.timer.isActive():
            self.timer.stop()
            self.camera.release()
            self.video_frame_item.setPixmap(QPixmap())
            self.btn_toggle_cam.setText("Start Camera Feed")
        else:
            self.camera_index = self.camera_selector.currentData()
            self.camera = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
            self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            if self.camera.isOpened():
                self.timer.start(30)
                self.btn_toggle_cam.setText("Stop Camera Feed")

    def spawn_tool_roi(self, x, y):
        selected_tool = self.tool_selector.currentText()
        self.tool_count += 1
        t_name = f"tool{self.tool_count:02d}_{selected_tool[:4]}"

        roi = SearchROI(x - 125, y - 125, tool_name=t_name, tool_type=selected_tool)
        self.cam_scene.addItem(roi)

        list_item = QListWidgetItem(t_name)
        list_item.setFlags(list_item.flags() | Qt.ItemFlag.ItemIsEditable)
        list_item.setData(Qt.ItemDataRole.UserRole, roi)

        self.active_tools_list.blockSignals(True)
        self.active_tools_list.addItem(list_item)
        self.active_tools_list.blockSignals(False)
        self.tool_states[t_name] = False

    def sync_scene_to_list(self):
        selected = self.cam_scene.selectedItems()
        if not selected:
            self.active_tools_list.clearSelection();
            self.build_empty_properties();
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
            self.cam_scene.blockSignals(True);
            self.cam_scene.clearSelection()
            roi.setSelected(True);
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

    def delete_selected_tool(self):
        curr_item = self.active_tools_list.currentItem()
        if curr_item:
            roi = curr_item.data(Qt.ItemDataRole.UserRole)
            if roi in self.cam_scene.items(): self.cam_scene.removeItem(roi)
            if roi.tool_name in self.tool_states: del self.tool_states[roi.tool_name]
            self.active_tools_list.takeItem(self.active_tools_list.row(curr_item))
            self.build_empty_properties()

    # --- PROJECT SAVE/LOAD LOGIC ---
    def save_project(self):
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Observatory Project", "", "JSON Files (*.json)")
        if not file_path: return

        project_data = {
            "camera_index": self.camera_selector.currentData(),
            "tools": []
        }

        for item in self.cam_scene.items():
            if hasattr(item, 'tool_type'):
                t_data = {
                    "name": item.tool_name, "type": item.tool_type,
                    "x": item.pos().x(), "y": item.pos().y(),
                    "w": item.rect().width(), "h": item.rect().height(),
                    "sensitivity": item.sensitivity, "threshold": item.threshold, "min_area": item.min_area,
                    "use_thresh": item.use_thresh, "filter_thresh": item.filter_thresh,
                    "use_edge": item.use_edge, "filter_edge": item.filter_edge,
                    "use_depth": item.use_depth, "filter_depth": item.filter_depth,
                    "ref_frame_b64": img_to_b64(item.reference_frame),
                    "template_b64": img_to_b64(item.trained_template)
                }
                if item.pattern_roi:
                    t_data["pattern"] = {"x": item.pattern_roi.pos().x(), "y": item.pattern_roi.pos().y(),
                                         "w": item.pattern_roi.rect().width(), "h": item.pattern_roi.rect().height()}
                project_data["tools"].append(t_data)

        with open(file_path, 'w') as f:
            json.dump(project_data, f)
        self.logger(f"Project saved successfully to {file_path}")

    def load_project(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Open Observatory Project", "", "JSON Files (*.json)")
        if not file_path: return

        with open(file_path, 'r') as f:
            data = json.load(f)

        # Stop camera, clear canvas
        if self.timer.isActive(): self.toggle_camera()
        while self.active_tools_list.count() > 0:
            self.active_tools_list.setCurrentRow(0);
            self.delete_selected_tool()

        # Set Camera and Start
        idx = self.camera_selector.findData(data.get("camera_index", 0))
        if idx >= 0: self.camera_selector.setCurrentIndex(idx)
        self.toggle_camera()

        # Rebuild Tools
        for t in data.get("tools", []):
            roi = SearchROI(t["x"], t["y"], tool_name=t["name"], tool_type=t["type"])
            roi.update_size(t["w"], t["h"])
            roi.sensitivity = t.get("sensitivity", 50)
            roi.threshold = t.get("threshold", 127)
            roi.min_area = t.get("min_area", 100)

            # Load Filters
            roi.use_thresh = t.get("use_thresh", False);
            roi.filter_thresh = t.get("filter_thresh", 127)
            roi.use_edge = t.get("use_edge", False);
            roi.filter_edge = t.get("filter_edge", 100)
            roi.use_depth = t.get("use_depth", False);
            roi.filter_depth = t.get("filter_depth", 5)

            # Rebuild Images
            roi.reference_frame = b64_to_img(t.get("ref_frame_b64", None))
            roi.trained_template = b64_to_img(t.get("template_b64", None))

            if roi.pattern_roi and "pattern" in t:
                roi.pattern_roi.setPos(t["pattern"]["x"], t["pattern"]["y"])
                roi.pattern_roi.update_size(t["pattern"]["w"], t["pattern"]["h"])

            self.cam_scene.addItem(roi)
            list_item = QListWidgetItem(roi.tool_name)
            list_item.setFlags(list_item.flags() | Qt.ItemFlag.ItemIsEditable)
            list_item.setData(Qt.ItemDataRole.UserRole, roi)
            self.active_tools_list.addItem(list_item)
            self.tool_states[roi.tool_name] = False
            self.tool_count += 1

        self.logger(f"Project loaded successfully from {file_path}")


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_THEME)
    observatory = ObservatoryEngine(logger=print)
    observatory.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()