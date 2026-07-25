import sys
import os
import time
import xml.etree.ElementTree as ET

# Silence OpenCV C++ probing errors
os.environ["OPENCV_LOG_LEVEL"] = "FATAL"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"
os.environ["OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"] = "0"

import cv2
import numpy as np
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QGraphicsView, QGraphicsScene,
                             QLabel, QSplitter, QComboBox, QToolBar, QStatusBar,
                             QMenuBar, QTreeWidget, QTreeWidgetItem, QTextBrowser,
                             QHeaderView, QFileDialog, QGraphicsTextItem, QFormLayout,
                             QLineEdit, QSpinBox, QCheckBox, QFontComboBox, QGroupBox,
                             QSizePolicy, QListWidget, QSlider, QGraphicsRectItem,
                             QGraphicsItem, QColorDialog, QListWidgetItem, QTreeWidgetItemIterator,
                             QGraphicsPolygonItem, QGraphicsEllipseItem)
from PyQt6.QtCore import pyqtSignal, QTimer, Qt, QRectF, QPointF, QRect
from PyQt6.QtGui import QColor, QBrush, QImage, QPixmap, QAction, QFont, QPen, QPainter, QPolygonF

# Try to import MediaPipe for Human Rigging
try:
    import mediapipe as mp

    # 🛑 DIAGNOSTIC LINE: This tells us exactly which file Python is loading
    print(f"\n--- MEDIAPIPE LOADED FROM: {mp.__file__} ---\n")

    mp_pose = mp.solutions.pose
    mp_drawing = mp.solutions.drawing_utils
    MP_AVAILABLE = True
except Exception as e:
    print(f"\n--- MEDIAPIPE DISABLED DUE TO ERROR: {e} ---\n")
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
QTreeWidget::item { padding: 4px; border-bottom: 1px solid #2b2b2b; }
QTreeWidget::item:selected { background-color: #2f65ca; color: white; }
QHeaderView::section { background-color: #3C3F41; padding: 4px; border: 1px solid #1e1e1e; font-weight: bold; }
QTextBrowser { background-color: #1e1e1e; color: #a9b7c6; border: 1px solid #555555; font-family: 'Consolas', monospace; }
QComboBox, QSpinBox, QLineEdit { background-color: #3C3F41; border: 1px solid #1e1e1e; padding: 3px; color: #ffffff; }
QMenuBar { background-color: #3C3F41; border-bottom: 1px solid #1e1e1e; }
QGroupBox { border: 1px solid #555555; margin-top: 10px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
QSlider::groove:horizontal { border: 1px solid #555; height: 8px; background: #3C3F41; margin: 2px 0; border-radius: 4px; }
QSlider::handle:horizontal { background: #4C5052; border: 1px solid #5C5C42; width: 14px; margin: -4px 0; border-radius: 7px; }
"""


# ---------------------------------------------------------
# 1. CUSTOM PROJECTOR CANVAS ASSETS & DATA STRUCTURES
# ---------------------------------------------------------
class FlowControlData:
    def __init__(self, action="End Program Execution", target_step=1):
        self.action = action
        self.target_step = target_step


class InteractiveTextItem(QGraphicsTextItem):
    def __init__(self, text="New Text Line Input"):
        super().__init__(text)
        self.setFlags(QGraphicsTextItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsTextItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsTextItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setDefaultTextColor(QColor("white"))
        self.setFont(QFont("Arial", 48))
        self.is_blinking = False
        self.tied_variable = "None"
        self.linked_obs_file = ""
        self.trigger_condition = "On True"
        self.display_duration = 10
        self._is_rotating = False
        self.update_transform_origin()

    def update_transform_origin(self):
        rect = self.boundingRect()
        self.setTransformOriginPoint(rect.width() / 2, rect.height() / 2)

    def setPlainText(self, text):
        super().setPlainText(text)
        self.update_transform_origin()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_rotating = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._is_rotating:
            delta_y = event.pos().y() - event.lastPos().y()
            new_angle = (self.rotation() + delta_y) % 360
            self.setRotation(new_angle)
            self.scene().update()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_rotating = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.scene().selectionChanged.emit()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class InteractiveMediaItem(QGraphicsRectItem):
    def __init__(self, filepath="Placeholder", media_type="Image"):
        super().__init__(0, 0, 200, 150)
        self.filepath = filepath
        self.media_type = media_type

        if media_type == "Video":
            color_hex, icon = "#3498db", "🎬"
        elif media_type == "Audio":
            color_hex, icon = "#e67e22", "🎵"
        else:
            color_hex, icon = "#2ecc71", "🎨"

        self.setPen(QPen(QColor(color_hex), 2))
        self.setBrush(QBrush(QColor(40, 40, 40, 200)))
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)

        self.text_tag = QGraphicsTextItem(f"{icon} {os.path.basename(filepath)}", self)
        self.text_tag.setDefaultTextColor(QColor("white"))
        self.text_tag.setPos(5, 5)

        self.is_blinking = False
        self.tied_variable = "None"
        self.linked_obs_file = ""
        self.trigger_condition = "On True"
        self.display_duration = 10


# ---------------------------------------------------------
# 2. THE PROJECTOR CANVAS
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
# 3. OBSERVATORY ENGINE (Vision Processing Workspace)
# ---------------------------------------------------------

# --- Handles & Interactive Elements ---
class ROIResizeHandle(QGraphicsRectItem):
    def __init__(self, parent, color_string):
        super().__init__(0, 0, 12, 12, parent)
        self.setBrush(QBrush(QColor(color_string)))
        self.setPen(QPen(QColor("white"), 1))
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._dragging = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True;
            event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            pos_in_parent = self.parentItem().mapFromItem(self, event.pos())
            self.parentItem().update_size(max(30, pos_in_parent.x()), max(30, pos_in_parent.y()))
            event.accept()

    def mouseReleaseEvent(self, event):
        self._dragging = False;
        event.accept()


class CornerHandle(QGraphicsEllipseItem):
    def __init__(self, parent, index):
        super().__init__(-6, -6, 12, 12, parent)
        self.index = index
        self.setBrush(QBrush(QColor("#e74c3c")))
        self.setPen(QPen(QColor("white"), 1))
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            self.parentItem().update_corner(self.index, value)
        return super().itemChange(change, value)


# --- Standard Tool ROI ---
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
            parent_w, parent_h = self.parentItem().rect().width(), self.parentItem().rect().height()
            self.setRect(0, 0, max(20, min(w, parent_w - self.pos().x())), max(20, min(h, parent_h - self.pos().y())))
            self.update_handle_pos()

    def update_handle_pos(self):
        self.handle.setPos(self.rect().width() - 6, self.rect().height() - 6)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.parentItem():
            r, pw, ph = self.rect(), self.parentItem().rect().width(), self.parentItem().rect().height()
            return QPointF(max(0, min(value.x(), pw - r.width())), max(0, min(value.y(), ph - r.height())))
        return super().itemChange(change, value)


class SearchROI(QGraphicsRectItem):
    def __init__(self, x, y, tool_name, tool_type):
        super().__init__(0, 0, 250, 250)
        self.tool_name = tool_name
        self.tool_type = tool_type

        # Tool Independent Memory Matrix
        self.threshold = 127
        self.sensitivity = 50
        self.current_score = 0
        self.is_triggered = False
        self.reference_frame = None
        self.trained_template = None

        self.setPos(x, y)
        color = "#3498db" if tool_type == "Motion Detection" else "white"
        self.setPen(QPen(QColor(color), 2, Qt.PenStyle.DashLine))
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        self.handle = ROIResizeHandle(self, color)
        self.update_handle_pos()

        if self.tool_type == "Pattern Match":
            self.pattern_roi = PatternROI(self)
        else:
            self.pattern_roi = None

        self.label = QGraphicsTextItem(f"{tool_name}\n[{tool_type}]", self)
        self.label.setDefaultTextColor(QColor(color))
        self.label.setPos(5, 5)

    def update_size(self, w, h):
        self.setRect(0, 0, w, h)
        self.update_handle_pos()
        if self.pattern_roi:
            p_rect = self.pattern_roi.rect()
            self.pattern_roi.setPos(min(self.pattern_roi.pos().x(), w - p_rect.width()),
                                    min(self.pattern_roi.pos().y(), h - p_rect.height()))

    def update_handle_pos(self):
        self.handle.setPos(self.rect().width() - 6, self.rect().height() - 6)


# --- Perspective Reference Plane ROI ---
class PerspectivePlaneROI(QGraphicsPolygonItem):
    def __init__(self, x, y, tool_name):
        super().__init__()
        self.tool_name = tool_name
        self.tool_type = "Reference Plane"

        self.current_score = 100
        self.sensitivity = 0
        self.homography_matrix = None

        # Default 4 corners forming a rectangle
        self.points = [QPointF(0, 0), QPointF(200, 0), QPointF(200, 200), QPointF(0, 200)]
        self.update_visual_polygon()

        self.setPos(x, y)
        self.setPen(QPen(QColor("#f39c12"), 2, Qt.PenStyle.DashLine))
        self.setBrush(QBrush(QColor(243, 156, 18, 40)))
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        self.handles = [CornerHandle(self, i) for i in range(4)]
        self.reposition_handles()

        self.label = QGraphicsTextItem(f"{tool_name}\n[Reference Plane]", self)
        self.label.setDefaultTextColor(QColor("#f39c12"))
        self.label.setPos(20, 20)

    def update_corner(self, index, pos):
        self.points[index] = pos
        self.update_visual_polygon()

    def update_visual_polygon(self):
        poly = QPolygonF(self.points)
        self.setPolygon(poly)

    def reposition_handles(self):
        for i, h in enumerate(self.handles):
            h.setPos(self.points[i])


# --- Vision Canvas Views ---
class VisionCanvasView(QGraphicsView):
    double_clicked = pyqtSignal(float, float)

    def __init__(self, scene):
        super().__init__(scene)

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
                         QColor(0, 200, 0, 180) if self.score < self.threshold else QColor(220, 0, 0, 180))

        t_x = int((self.threshold / 100.0) * rect.width())
        painter.setPen(QPen(QColor("white"), 3))
        painter.drawLine(t_x, 0, t_x, rect.height())

        painter.setPen(QColor("white"))
        painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                         f"Match: {self.score}%  |  Trigger Line: {self.threshold}%")

    def mousePressEvent(self, event):
        self.update_threshold_from_mouse(event.pos().x())
        self._dragging = True

    def mouseMoveEvent(self, event):
        if self._dragging: self.update_threshold_from_mouse(event.pos().x())

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def update_threshold_from_mouse(self, x):
        self.threshold = max(0, min(100, int((x / self.width()) * 100)))
        self.update()


# ---------------------------------------------------------
# THE OBSERVATORY (Vision Environment)
# ---------------------------------------------------------
class ObservatoryEngine(QMainWindow):
    def __init__(self, logger):
        super().__init__()
        self.logger = logger
        self.setWindowTitle("Observatory - Modular Vision Environment")
        self.setGeometry(150, 150, 1350, 800)

        self.camera = None
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.tool_count = 0
        self.current_raw_frame = None

        self.tool_states = {"None": True}
        self.prev_tool_states = self.tool_states.copy()

        if MP_AVAILABLE:
            self.pose_tracker = mp_pose.Pose(min_detection_confidence=0.5, min_tracking_confidence=0.5)

        self.setup_ui()

    def setup_ui(self):
        main_layout = QVBoxLayout()
        central_widget = QWidget();
        central_widget.setLayout(main_layout);
        self.setCentralWidget(central_widget)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Devices: "))
        self.camera_selector = QComboBox()
        self.populate_cameras()
        toolbar.addWidget(self.camera_selector)

        self.btn_toggle_cam = QPushButton("Start Camera")
        self.btn_toggle_cam.clicked.connect(self.toggle_camera)
        toolbar.addWidget(self.btn_toggle_cam)
        toolbar.addStretch()
        main_layout.addLayout(toolbar)

        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left Panel (Tools List)
        left_panel = QWidget();
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(QLabel("<b>Vision Tool Arsenal</b>"))

        self.tool_selector = QComboBox()
        available_tools = ["Pattern Match", "Motion Detection", "Blob Detection", "Reference Plane"]
        if MP_AVAILABLE: available_tools.append("Human Rigging (Pose)")
        self.tool_selector.addItems(available_tools)
        left_layout.addWidget(self.tool_selector)
        left_layout.addWidget(QLabel("<small><i>Double-click video feed to drop selected tool.</i></small>"))

        self.active_tools_list = QListWidget()
        self.active_tools_list.itemChanged.connect(self.handle_tool_rename)
        self.active_tools_list.itemSelectionChanged.connect(self.sync_list_to_scene)
        left_layout.addWidget(self.active_tools_list)

        self.btn_delete_tool = QPushButton("🗑️ Delete Tool")
        self.btn_delete_tool.clicked.connect(self.delete_selected_tool)
        left_layout.addWidget(self.btn_delete_tool)
        h_splitter.addWidget(left_panel)

        # Center Panel (Video Canvas)
        self.cam_scene = QGraphicsScene()
        self.cam_scene.selectionChanged.connect(self.sync_scene_to_list)
        self.cam_view = VisionCanvasView(self.cam_scene)
        self.cam_view.setStyleSheet("background-color: #1e1e1e; border: 1px solid #555;")
        self.cam_view.double_clicked.connect(self.spawn_tool_roi)
        self.video_frame_item = self.cam_scene.addPixmap(QPixmap())
        h_splitter.addWidget(self.cam_view)

        # Right Panel (Dynamic Properties)
        right_panel = QWidget();
        self.right_layout = QVBoxLayout(right_panel)
        self.right_layout.addWidget(QLabel("<b>Tool Inspector</b>"))

        self.tool_prop_group = QGroupBox("Selected Tool Properties")
        self.tool_prop_layout = QFormLayout(self.tool_prop_group)
        self.right_layout.addWidget(self.tool_prop_group)

        self.score_group = QGroupBox("Dynamic Trigger Output")
        score_layout = QVBoxLayout(self.score_group)
        self.score_bar = ThresholdScoreBar()
        self.score_bar.trigger_state_changed.connect(self.sync_threshold_to_tool)
        score_layout.addWidget(self.score_bar)
        self.right_layout.addWidget(self.score_group)

        self.right_layout.addStretch()
        h_splitter.addWidget(right_panel)

        h_splitter.setSizes([250, 800, 300])
        main_layout.addWidget(h_splitter)
        self.build_empty_properties()

    # --- Property Inspector Dynamics ---
    def build_empty_properties(self):
        self.clear_layout(self.tool_prop_layout)
        self.tool_prop_layout.addRow(QLabel("<i>Select a tool to view properties.</i>"))
        self.score_group.setVisible(False)

    def load_tool_properties_to_ui(self, tool_item):
        self.clear_layout(self.tool_prop_layout)
        self.score_group.setVisible(True)

        self.score_bar.blockSignals(True)
        self.score_bar.threshold = tool_item.sensitivity
        self.score_bar.set_score(tool_item.current_score)
        self.score_bar.blockSignals(False)

        type_label = QLabel(f"<b>{tool_item.tool_type}</b>")
        type_label.setStyleSheet("color: #3498db;")
        self.tool_prop_layout.addRow("Type:", type_label)

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

        elif tool_item.tool_type == "Motion Detection":
            slider_thresh = QSlider(Qt.Orientation.Horizontal)
            slider_thresh.setRange(1, 255)
            slider_thresh.setValue(tool_item.threshold)
            slider_thresh.valueChanged.connect(lambda v: setattr(tool_item, 'threshold', v))
            self.tool_prop_layout.addRow("Pixel Diff Thresh:", slider_thresh)

            btn_ref = QPushButton("📷 Set Reference Frame")
            btn_ref.clicked.connect(lambda: self.set_motion_reference(tool_item))
            self.tool_prop_layout.addRow(btn_ref)

            status = QLabel("Ready" if tool_item.reference_frame is not None else "Missing Frame")
            self.tool_prop_layout.addRow("Status:", status)

        elif tool_item.tool_type == "Blob Detection":
            slider_area = QSlider(Qt.Orientation.Horizontal)
            slider_area.setRange(10, 5000)
            slider_area.setValue(tool_item.threshold)
            slider_area.valueChanged.connect(lambda v: setattr(tool_item, 'threshold', v))
            self.tool_prop_layout.addRow("Min Blob Area:", slider_area)

        elif tool_item.tool_type == "Reference Plane":
            self.score_group.setVisible(False)
            btn_calc = QPushButton("📐 Calculate Perspective Matrix")
            btn_calc.clicked.connect(lambda: self.calculate_homography(tool_item))
            self.tool_prop_layout.addRow(btn_calc)

            status = QLabel("Matrix Active" if tool_item.homography_matrix is not None else "Pending Calculation")
            self.tool_prop_layout.addRow("Matrix:", status)

    def clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()

    # --- Tool Specific Actions ---
    def sync_threshold_to_tool(self, triggered):
        items = self.cam_scene.selectedItems()
        if items and hasattr(items[0], 'sensitivity'):
            items[0].sensitivity = self.score_bar.threshold

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
            self.load_tool_properties_to_ui(tool_item)  # Refresh UI thumbnail

    def set_motion_reference(self, tool_item):
        if self.current_raw_frame is None: return
        x1, y1 = int(max(0, tool_item.pos().x())), int(max(0, tool_item.pos().y()))
        x2, y2 = int(min(self.current_raw_frame.shape[1], x1 + tool_item.rect().width())), int(
            min(self.current_raw_frame.shape[0], y1 + tool_item.rect().height()))

        if x2 > x1 and y2 > y1:
            gray = cv2.cvtColor(self.current_raw_frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
            tool_item.reference_frame = cv2.GaussianBlur(gray, (21, 21), 0)
            self.load_tool_properties_to_ui(tool_item)

    def calculate_homography(self, tool_item):
        # Maps the 4 custom polygon points to a perfect 500x500 square matrix
        pts_src = np.array([[p.x() + tool_item.pos().x(), p.y() + tool_item.pos().y()] for p in tool_item.points],
                           dtype="float32")
        pts_dst = np.array([[0, 0], [500, 0], [500, 500], [0, 500]], dtype="float32")
        matrix = cv2.getPerspectiveTransform(pts_src, pts_dst)
        tool_item.homography_matrix = matrix
        self.load_tool_properties_to_ui(tool_item)
        self.logger(f"Observatory: Calculated Homography for {tool_item.tool_name}")

    # --- Sync Scene/List ---
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

    # --- Core Vision Loop ---
    def update_frame(self):
        if self.camera and self.camera.isOpened():
            ret, frame = self.camera.read()
            if not ret: return

            self.current_raw_frame = frame.copy()
            display_frame = frame.copy()
            gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            blur_frame = cv2.GaussianBlur(gray_frame, (21, 21), 0)

            self.prev_tool_states = self.tool_states.copy()
            active_item = self.cam_scene.selectedItems()[0] if self.cam_scene.selectedItems() else None

            for item in self.cam_scene.items():
                if not hasattr(item, 'tool_type'): continue

                # Reference Plane Logic
                if item.tool_type == "Reference Plane":
                    pts = np.array([[p.x() + item.pos().x(), p.y() + item.pos().y()] for p in item.points], np.int32)
                    cv2.polylines(display_frame, [pts], isClosed=True, color=(0, 165, 255), thickness=2)

                    if item.homography_matrix is not None:
                        # Draw center coordinate axis to visualize the plane
                        cx, cy = int(np.mean(pts[:, 0])), int(np.mean(pts[:, 1]))
                        cv2.circle(display_frame, (cx, cy), 5, (255, 255, 0), -1)
                    continue

                # Standard ROI Bounds
                x1, y1 = int(max(0, item.pos().x())), int(max(0, item.pos().y()))
                x2, y2 = int(min(frame.shape[1], x1 + item.rect().width())), int(
                    min(frame.shape[0], y1 + item.rect().height()))
                if x2 <= x1 or y2 <= y1: continue

                roi_gray = blur_frame[y1:y2, x1:x2]

                # Pattern Match
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

                # Motion Detection
                elif item.tool_type == "Motion Detection" and item.reference_frame is not None:
                    if roi_gray.shape == item.reference_frame.shape:
                        delta = cv2.absdiff(item.reference_frame, roi_gray)
                        _, thresh = cv2.threshold(delta, item.threshold, 255, cv2.THRESH_BINARY)
                        non_zero = cv2.countNonZero(thresh)
                        motion_pct = int((non_zero / (thresh.shape[0] * thresh.shape[1])) * 100)
                        item.current_score = motion_pct

                        if motion_pct >= item.sensitivity:
                            self.tool_states[item.tool_name] = True
                            cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
                        else:
                            self.tool_states[item.tool_name] = False

                # Blob Detection (Scaffolding setup)
                elif item.tool_type == "Blob Detection":
                    params = cv2.SimpleBlobDetector_Params()
                    params.filterByArea = True
                    params.minArea = item.threshold
                    detector = cv2.SimpleBlobDetector_create(params)
                    keypoints = detector.detect(
                        cv2.bitwise_not(gray_frame[y1:y2, x1:x2]))  # Invert for standard black blobs on white
                    item.current_score = len(keypoints)

                    if item.current_score >= item.sensitivity:
                        self.tool_states[item.tool_name] = True
                        for kp in keypoints:
                            cv2.circle(display_frame, (int(x1 + kp.pt[0]), int(y1 + kp.pt[1])), int(kp.size),
                                       (255, 0, 255), 2)
                    else:
                        self.tool_states[item.tool_name] = False

                # Human Pose
                elif item.tool_type == "Human Rigging (Pose)" and MP_AVAILABLE:
                    roi_rgb = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
                    results = self.pose_tracker.process(roi_rgb)
                    if results.pose_landmarks:
                        mp_drawing.draw_landmarks(display_frame[y1:y2, x1:x2], results.pose_landmarks,
                                                  mp_pose.POSE_CONNECTIONS)
                        item.current_score = 100
                        self.tool_states[item.tool_name] = True
                    else:
                        item.current_score = 0
                        self.tool_states[item.tool_name] = False

                if item == active_item:
                    self.score_bar.set_score(item.current_score)

            rgb_image = cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb_image.shape
            self.video_frame_item.setPixmap(
                QPixmap.fromImage(QImage(rgb_image.data, w, h, ch * w, QImage.Format.Format_RGB888)))

    # --- Tool Management ---
    def populate_cameras(self):
        self.camera_selector.clear()
        for i in range(4):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened(): self.camera_selector.addItem(f"Camera {i} (DirectShow)", (i, "DSHOW")); cap.release()
        if self.camera_selector.count() == 0: self.camera_selector.addItem("No Cameras Found", (0, "NONE"))

    def toggle_camera(self):
        if self.timer.isActive():
            self.timer.stop();
            self.camera.release();
            self.video_frame_item.setPixmap(QPixmap());
            self.btn_toggle_cam.setText("Start Camera")
        else:
            data = self.camera_selector.currentData()
            if data and data[1] != "NONE":
                self.camera = cv2.VideoCapture(data[0], cv2.CAP_DSHOW)
                self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280);
                self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                if self.camera.isOpened(): self.timer.start(30); self.btn_toggle_cam.setText("Stop Camera")

    def spawn_tool_roi(self, x, y):
        selected_tool = self.tool_selector.currentText()
        self.tool_count += 1
        t_name = f"tool{self.tool_count:02d}_{selected_tool[:4]}"

        if selected_tool == "Reference Plane":
            roi = PerspectivePlaneROI(x - 100, y - 100, tool_name=t_name)
        else:
            roi = SearchROI(x - 125, y - 125, tool_name=t_name, tool_type=selected_tool)

        self.cam_scene.addItem(roi)

        list_item = QListWidgetItem(t_name)
        list_item.setFlags(list_item.flags() | Qt.ItemFlag.ItemIsEditable)
        list_item.setData(Qt.ItemDataRole.UserRole, roi)

        self.active_tools_list.blockSignals(True)
        self.active_tools_list.addItem(list_item)
        self.active_tools_list.blockSignals(False)
        self.tool_states[t_name] = False

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
            if roi and roi in self.cam_scene.items(): self.cam_scene.removeItem(roi)
            if roi.tool_name in self.tool_states: del self.tool_states[roi.tool_name]
            self.active_tools_list.takeItem(self.active_tools_list.row(curr_item))
            self.build_empty_properties()


# ---------------------------------------------------------
# 4. THE AUTHORING STUDIO ENVIRONMENT (Execution Workspace)
# ---------------------------------------------------------
class StudioCanvasView(QGraphicsView):
    file_dropped = pyqtSignal(str, str)

    def __init__(self, scene, parent_interface):
        super().__init__(scene);
        self.parent_interface = parent_interface;
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
                self.file_dropped.emit(url.toLocalFile(), t)
            event.acceptProposedAction()


class AuthoringInterface(QMainWindow):
    def __init__(self, canvas):
        super().__init__()
        self.setWindowTitle("LightGuide Studio - Authoring Environment")
        self.setGeometry(200, 200, 1500, 850)

        self.canvas, self.studio_scene = canvas, canvas.scene
        self.active_item = None
        self.is_running = False
        self.execution_sequence, self.pc, self.wait_state, self.timer_end = [], 0, "IDLE", 0

        self.setup_ui()
        self.observatory = ObservatoryEngine(self.log_message)

        self.step_tree.itemSelectionChanged.connect(self.handle_tree_selection)
        self.studio_scene.selectionChanged.connect(self.handle_canvas_selection)

        self.blink_state = True
        self.master_clock = QTimer();
        self.master_clock.timeout.connect(self.process_execution_engine);
        self.master_clock.start(100)
        self.enter_design_mode()

    def setup_ui(self):
        main_layout = QVBoxLayout()
        central = QWidget();
        central.setLayout(main_layout);
        self.setCentralWidget(central)

        toolbar = QHBoxLayout()
        self.btn_design_mode = QPushButton("✏️ DESIGN MODE")
        self.btn_design_mode.clicked.connect(self.enter_design_mode)
        self.btn_run_mode = QPushButton("▶️ RUN MODE")
        self.btn_run_mode.clicked.connect(self.enter_run_mode)
        btn_obs = QPushButton("👁️ Launch Observatory Vision")
        btn_obs.clicked.connect(lambda: self.observatory.show())

        toolbar.addWidget(self.btn_design_mode);
        toolbar.addWidget(self.btn_run_mode)
        toolbar.addStretch();
        toolbar.addWidget(btn_obs);
        main_layout.addLayout(toolbar)

        v_splitter, h_splitter = QSplitter(Qt.Orientation.Vertical), QSplitter(Qt.Orientation.Horizontal)

        left = QWidget();
        l_lay = QVBoxLayout(left)
        btn_step = QPushButton("🟦 Add Step Block");
        btn_step.clicked.connect(self.add_new_step_node)
        btn_txt = QPushButton("+ Insert Text Element");
        btn_txt.clicked.connect(lambda: self.insert_asset(InteractiveTextItem("Text")))
        btn_img = QPushButton("+ Insert Image Asset");
        btn_img.clicked.connect(lambda: self.browse_media("Image"))
        btn_vid = QPushButton("+ Insert Video Asset");
        btn_vid.clicked.connect(lambda: self.browse_media("Video"))
        btn_flow = QPushButton("🛑 Insert Flow Control");
        btn_flow.clicked.connect(self.insert_flow_control)

        for b in [btn_step, btn_txt, btn_img, btn_vid, btn_flow]: l_lay.addWidget(b)
        l_lay.addStretch();
        h_splitter.addWidget(left)

        center = QWidget();
        c_lay = QVBoxLayout(center)
        self.step_tree = QTreeWidget()
        self.step_tree.setHeaderLabels(["Sequence Pipeline", "Action / Trigger", "Properties"])
        c_lay.addWidget(self.step_tree);
        h_splitter.addWidget(center)

        right = QWidget();
        r_lay = QVBoxLayout(right)
        self.prop_group = QGroupBox("Asset Properties")
        p_form = QFormLayout(self.prop_group)
        self.prop_text = QLineEdit();
        self.prop_text.textEdited.connect(self.update_active_item)
        self.prop_var = QComboBox();
        self.prop_var.currentTextChanged.connect(self.update_active_item)
        btn_link = QPushButton("📂 Link Vision Config");
        btn_link.clicked.connect(self.load_vars_from_file)

        var_lay = QHBoxLayout();
        var_lay.addWidget(self.prop_var);
        var_lay.addWidget(btn_link)
        p_form.addRow("Text Context:", self.prop_text);
        p_form.addRow("Trigger Variable:", var_lay)
        r_lay.addWidget(self.prop_group)
        r_lay.addStretch();
        h_splitter.addWidget(right)

        self.console = QTextBrowser();
        self.console.setReadOnly(True)
        v_splitter.addWidget(h_splitter);
        v_splitter.addWidget(self.console)
        main_layout.addWidget(v_splitter)

        self.studio_override_view = StudioCanvasView(self.studio_scene, self)
        self.canvas.setCentralWidget(self.studio_override_view)
        self.add_new_step_node()

    # --- Mode & Engine (Condensed for space) ---
    def enter_design_mode(self):
        self.is_running = False
        self.btn_design_mode.setStyleSheet("background-color: #2ecc71; font-weight: bold; color: black;")
        self.btn_run_mode.setStyleSheet("background-color: #3C3F41; color: white;")
        for item in self.studio_scene.items(): item.setVisible(True)

    def enter_run_mode(self):
        self.is_running = True
        self.btn_design_mode.setStyleSheet("background-color: #3C3F41; color: white;")
        self.btn_run_mode.setStyleSheet("background-color: #e74c3c; font-weight: bold; color: white;")
        for item in self.studio_scene.items(): item.setVisible(False)
        self.execution_sequence = self.build_execution_sequence()
        self.pc, self.wait_state = 0, "IDLE"

    def build_execution_sequence(self):
        seq = []
        for i in range(self.step_tree.topLevelItemCount()):
            top = self.step_tree.topLevelItem(i);
            seq.append({"type": "step_start"})
            for j in range(top.childCount()):
                asset = top.child(j).data(0, Qt.ItemDataRole.UserRole)
                if asset: seq.append({"type": "asset", "item": asset})
        return seq

    def process_execution_engine(self):
        self.blink_state = not self.blink_state
        if not self.is_running or self.pc >= len(self.execution_sequence): return
        cmd = self.execution_sequence[self.pc]

        if cmd["type"] == "step_start":
            for item in self.studio_scene.items(): item.setVisible(False)
            self.pc += 1;
            self.wait_state = "IDLE"

        elif cmd["type"] == "asset":
            item = cmd["item"]
            if self.wait_state == "IDLE":
                reg = self.observatory.tool_states
                if item.tied_variable == "None" or reg.get(item.tied_variable, False):
                    self.timer_end = time.time() + item.display_duration;
                    self.wait_state = "WAIT_DURATION"
            elif self.wait_state == "WAIT_DURATION":
                if time.time() < self.timer_end:
                    item.setVisible(self.blink_state if getattr(item, 'is_blinking', False) else True)
                else:
                    item.setVisible(False); self.pc += 1; self.wait_state = "IDLE"

    # --- UI Helpers ---
    def add_new_step_node(self):
        idx = self.step_tree.topLevelItemCount() + 1
        item = QTreeWidgetItem(self.step_tree, [f"Step Block [{idx}]"])
        item.setBackground(0, QColor("#1e508c"));
        item.setForeground(0, QColor("white"))
        item.setExpanded(True);
        self.step_tree.setCurrentItem(item)

    def insert_asset(self, item_obj, name="Asset"):
        if not self.step_tree.topLevelItemCount(): self.add_new_step_node()
        parent = self.step_tree.currentItem() if self.step_tree.currentItem() else self.step_tree.topLevelItem(0)
        if parent.parent(): parent = parent.parent()
        item_obj.setPos(200, 200);
        self.studio_scene.addItem(item_obj)
        ti = QTreeWidgetItem(parent, [name, "Wait For [None]", "Dur: 10s"]);
        ti.setData(0, Qt.ItemDataRole.UserRole, item_obj)

    def browse_media(self, mtype):
        path, _ = QFileDialog.getOpenFileName(self, f"Select {mtype}", "", "*.*")
        if path: self.insert_asset(InteractiveMediaItem(path, mtype), os.path.basename(path))

    def insert_flow_control(self):
        pass  # Stub for brevity

    def load_vars_from_file(self):
        self.prop_var.blockSignals(True)
        self.prop_var.clear();
        self.prop_var.addItems(["None"] + [t for t in self.observatory.tool_states.keys() if t != "None"])
        self.prop_var.blockSignals(False)

    def handle_tree_selection(self):
        sel = self.step_tree.selectedItems()
        if sel and sel[0].data(0, Qt.ItemDataRole.UserRole):
            self.studio_scene.clearSelection()
            if isinstance(sel[0].data(0, Qt.ItemDataRole.UserRole), QGraphicsItem):
                sel[0].data(0, Qt.ItemDataRole.UserRole).setSelected(True)

    def handle_canvas_selection(self):
        sel = self.studio_scene.selectedItems()
        if sel:
            self.active_item = sel[0]
            self.prop_var.setCurrentText(self.active_item.tied_variable)
            if isinstance(self.active_item, InteractiveTextItem):
                self.prop_text.setText(self.active_item.toPlainText())

    def update_active_item(self):
        if not self.active_item: return
        if isinstance(self.active_item, InteractiveTextItem): self.active_item.setPlainText(self.prop_text.text())
        self.active_item.tied_variable = self.prop_var.currentText()

    def log_message(self, msg):
        self.console.append(f"<span style='color:#00FF00;'>[{time.strftime('%H:%M:%S')}] {msg}</span>")


def main():
    app = QApplication(sys.argv);
    app.setStyleSheet(DARK_THEME)
    canvas = ProjectorCanvas();
    authoring = AuthoringInterface(canvas)
    canvas.show();
    authoring.show();
    sys.exit(app.exec())


if __name__ == "__main__": main()