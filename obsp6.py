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
from PyQt6.QtCore import pyqtSignal, QTimer, Qt, QPointF, QRect, QThread
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
QListWidget { background-color: #313335; border: 1px solid #1e1e1e; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QTextEdit { background-color: #3C3F41; border: 1px solid #1e1e1e; padding: 3px; color: #ffffff; }
QMenuBar { background-color: #3C3F41; border-bottom: 1px solid #1e1e1e; }
QMenuBar::item:selected { background-color: #2f65ca; }
QMenu { background-color: #313335; border: 1px solid #555; }
QMenu::item:selected { background-color: #2f65ca; }
QGroupBox { border: 1px solid #555555; margin-top: 10px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
QSlider::groove:horizontal { border: 1px solid #555; height: 8px; background: #3C3F41; margin: 2px 0; border-radius: 4px; }
QSlider::handle:horizontal { background: #4C5052; border: 1px solid #5C5C42; width: 14px; margin: -4px 0; border-radius: 7px; }
"""


def img_to_b64(img):
    if img is None: return None
    _, buffer = cv2.imencode('.png', img)
    return base64.b64encode(buffer).decode('utf-8')


def b64_to_img(b64_str):
    if not b64_str: return None
    img_data = base64.b64decode(b64_str)
    nparr = np.frombuffer(img_data, np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)


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
            self.parentItem().update_size(max(40, pos_in_parent.x()), max(40, pos_in_parent.y()))
            event.accept()

    def mouseReleaseEvent(self, event):
        self._dragging = False; event.accept()


class CornerHandle(QGraphicsEllipseItem):
    def __init__(self, parent, index):
        super().__init__(-8, -8, 16, 16, parent)
        self.index = index
        self.setBrush(QBrush(QColor("#f39c12")))
        self.setPen(QPen(QColor("white"), 1))
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        self.lbl = QGraphicsTextItem(str(index + 1), self)
        self.lbl.setDefaultTextColor(QColor("black"))
        self.lbl.setFont(QFont("Arial", 8, QFont.Weight.Bold))
        self.lbl.setPos(-4, -6)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            self.parentItem().update_corner(self.index, value)
        return super().itemChange(change, value)


class BlobOriginHandle(QGraphicsEllipseItem):
    def __init__(self, parent):
        super().__init__(-6, -6, 12, 12, parent)
        self.setBrush(QBrush(QColor(0, 255, 0, 100)))
        self.setPen(QPen(QColor("green"), 2))
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setPos(parent.rect().width() / 2, parent.rect().height() / 2)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.parentItem():
            new_x = max(0, min(value.x(), self.parentItem().rect().width()))
            new_y = max(0, min(value.y(), self.parentItem().rect().height()))
            return QPointF(new_x, new_y)
        return super().itemChange(change, value)


class PerspectivePlaneROI(QGraphicsPolygonItem):
    def __init__(self, x, y, tool_name):
        super().__init__()
        self.tool_name = tool_name
        self.tool_type = "Reference Plane"

        self.current_score = 100
        self.sensitivity = 0

        self.d1_2 = 12.0
        self.d2_3 = 12.0
        self.d3_4 = 12.0
        self.d4_1 = 12.0

        self.points = [QPointF(0, 0), QPointF(200, 0), QPointF(200, 200), QPointF(0, 200)]
        self.update_visual_polygon()

        self.setPos(x, y)
        self.setPen(QPen(QColor("#f39c12"), 2, Qt.PenStyle.DashLine))
        self.setBrush(QBrush(QColor(243, 156, 18, 40)))
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        self.handles = [CornerHandle(self, i) for i in range(4)]
        self.reposition_handles()

        self.label = QGraphicsTextItem(f"{tool_name}\n[Calib Plane]", self)
        self.label.setDefaultTextColor(QColor("#f39c12"))
        self.label.setPos(20, 20)

    def update_corner(self, index, pos):
        self.points[index] = pos;
        self.update_visual_polygon()

    def update_visual_polygon(self):
        self.setPolygon(QPolygonF(self.points))

    def reposition_handles(self):
        for i, h in enumerate(self.handles): h.setPos(self.points[i])


class SearchROI(QGraphicsRectItem):
    def __init__(self, x, y, tool_name, tool_type):
        super().__init__(0, 0, 250, 250)
        self.tool_name = tool_name
        self.tool_type = tool_type

        # Core Properties
        self.threshold = 127
        self.sensitivity = 50
        self.min_area = 100

        # Blob Properties
        self.blob_color = 0  # 0=Dark, 255=Light
        self.blob_max_area = 5000
        self.last_blob_x, self.last_blob_y, self.last_blob_a = 0, 0, 0.0

        # LLM Properties
        self.llm_prompt = "Describe what is inside this image crop."
        self.llm_response = "Waiting for trigger..."
        self.llm_is_processing = False

        # State tracking (Raw vs Filtered)
        self.current_score = 0
        self.is_triggered = False
        self.raw_reference_frame = None;
        self.reference_frame = None
        self.raw_trained_template = None;
        self.trained_template = None

        # Filters
        self.use_thresh = False;
        self.filter_thresh = 127
        self.use_edge = False;
        self.filter_edge = 100
        self.use_depth = False;
        self.filter_depth = 5

        self.setPos(x, y)
        color_map = {"Delete (Missing Object)": "#e74c3c", "Motion Detection": "#3498db", "Blob Detection": "#9b59b6",
                     "LLM Vision": "#1abc9c"}
        color = color_map.get(tool_type, "white")

        self.setPen(QPen(QColor(color), 2, Qt.PenStyle.DashLine))
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        self.handle = ROIResizeHandle(self, color)
        self.update_handle_pos()

        self.origin_handle = BlobOriginHandle(self) if self.tool_type == "Blob Detection" else None

        self.label = QGraphicsTextItem(f"{tool_name}\n[{tool_type}]", self)
        self.label.setDefaultTextColor(QColor(color))
        self.label.setPos(5, 5)

    def update_size(self, w, h):
        self.setRect(0, 0, w, h)
        self.update_handle_pos()
        if self.origin_handle:
            self.origin_handle.setPos(min(self.origin_handle.pos().x(), w), min(self.origin_handle.pos().y(), h))

    def update_handle_pos(self):
        self.handle.setPos(self.rect().width() - 6, self.rect().height() - 6)


# --- LLM API MOCK WORKER ---
class LLMWorker(QThread):
    finished = pyqtSignal(str)

    def __init__(self, prompt, base64_image):
        super().__init__()
        self.prompt = prompt
        self.base64_image = base64_image

    def run(self):
        time.sleep(2)  # Simulate network delay
        # TODO: Replace with real HTTP request to your hosted LLM / Ollama
        response = f"[Simulated API Response]\nPrompt was: '{self.prompt}'\nProcessed image of length: {len(self.base64_image)} chars."
        self.finished.emit(response)


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
        painter.setPen(QPen(QColor("white"), 3));
        painter.drawLine(t_x, 0, t_x, rect.height())
        painter.setPen(QColor("white"));
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
        self.threshold = max(0, min(100, int((x / self.width()) * 100)));
        self.update()


class ObservatoryEngine(QMainWindow):
    def __init__(self, logger):
        super().__init__()
        self.logger = logger
        self.setWindowTitle("Observatory - Professional Vision Environment")
        self.setGeometry(150, 150, 1450, 850)

        self.camera = None
        self.camera_index = 0
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.tool_count = 0
        self.current_raw_frame = None

        self.tool_states = {"None": True}
        self.world_calibration_matrix = None
        self.llm_worker = None

        if MP_AVAILABLE:
            self.pose_tracker = mp_pose.Pose(min_detection_confidence=0.5, min_tracking_confidence=0.5)

        self.setup_ui()

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

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(QLabel("<b>Vision Tool Arsenal</b>"))

        self.tool_selector = QComboBox()
        available_tools = ["Pattern Match", "Blob Detection", "Delete (Missing Object)", "Motion Detection",
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
        self.right_layout.addWidget(QLabel("<b>Tool Inspector</b>"))

        self.filter_group = QGroupBox("Pre-Processing Filters")
        self.filter_layout = QFormLayout(self.filter_group)
        self.right_layout.addWidget(self.filter_group)

        self.tool_prop_group = QGroupBox("Tool Settings & Output")
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
        h_splitter.setSizes([250, 800, 350])
        main_layout.addWidget(h_splitter)

        self.lbl_template_thumb = QLabel("No Trained Image")
        self.lbl_template_thumb.setFixedSize(100, 60)
        self.lbl_template_thumb.setStyleSheet("background-color: #1e1e1e; border: 1px dashed #555;")
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
            if item.widget() and item.widget() != self.lbl_template_thumb:
                item.widget().deleteLater()
            elif item.layout():
                self.clear_layout(item.layout())

    def sync_threshold_to_tool(self, triggered):
        items = self.cam_scene.selectedItems()
        if items and hasattr(items[0], 'sensitivity'): items[0].sensitivity = self.score_bar.threshold

    def update_filter_val(self, item, attr, val):
        setattr(item, attr, val);
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
        if not hasattr(self, 'lbl_template_thumb'): return
        if item.trained_template is not None:
            rgb_crop = cv2.cvtColor(item.trained_template, cv2.COLOR_GRAY2RGB)
            h, w, ch = rgb_crop.shape
            img = QImage(rgb_crop.data, w, h, ch * w, QImage.Format.Format_RGB888)
            self.lbl_template_thumb.setPixmap(
                QPixmap.fromImage(img).scaled(100, 60, Qt.AspectRatioMode.KeepAspectRatio))

    def create_horiz_slider_row(self, item, bool_attr, val_attr, checkbox_label, slider_range):
        lay = QHBoxLayout()
        chk = QCheckBox(checkbox_label);
        chk.setChecked(getattr(item, bool_attr))
        chk.toggled.connect(lambda v, i=item, b=bool_attr: self.update_filter_val(i, b, v))
        sld = QSlider(Qt.Orientation.Horizontal);
        sld.setRange(slider_range[0], slider_range[1]);
        sld.setValue(getattr(item, val_attr))
        sld.valueChanged.connect(lambda v, i=item, a=val_attr: self.update_filter_val(i, a, v))
        lay.addWidget(chk);
        lay.addWidget(sld)
        return lay

    def load_tool_properties_to_ui(self, tool_item):
        self.clear_layout(self.filter_layout)
        self.clear_layout(self.tool_prop_layout)
        self.filter_group.setVisible(True)
        self.score_group.setVisible(True)

        self.score_bar.blockSignals(True)
        self.score_bar.threshold = tool_item.sensitivity
        self.score_bar.set_score(tool_item.current_score)
        self.score_bar.blockSignals(False)

        type_label = QLabel(f"<b>{tool_item.tool_type}</b>")
        type_label.setStyleSheet("color: #3498db;")
        self.tool_prop_layout.addRow("Tool Logic:", type_label)

        if tool_item.tool_type == "Reference Plane":
            self.filter_group.setVisible(False);
            self.score_group.setVisible(False)

            spin_d12 = QDoubleSpinBox();
            spin_d12.setRange(0.1, 1000.0);
            spin_d12.setValue(tool_item.d1_2)
            spin_d12.valueChanged.connect(lambda v, i=tool_item: setattr(i, 'd1_2', v))
            self.tool_prop_layout.addRow("Dist 1->2 (in):", spin_d12)

            spin_d23 = QDoubleSpinBox();
            spin_d23.setRange(0.1, 1000.0);
            spin_d23.setValue(tool_item.d2_3)
            spin_d23.valueChanged.connect(lambda v, i=tool_item: setattr(i, 'd2_3', v))
            self.tool_prop_layout.addRow("Dist 2->3 (in):", spin_d23)

            spin_d34 = QDoubleSpinBox();
            spin_d34.setRange(0.1, 1000.0);
            spin_d34.setValue(tool_item.d3_4)
            spin_d34.valueChanged.connect(lambda v, i=tool_item: setattr(i, 'd3_4', v))
            self.tool_prop_layout.addRow("Dist 3->4 (in):", spin_d34)

            spin_d41 = QDoubleSpinBox();
            spin_d41.setRange(0.1, 1000.0);
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
            self.update_template_thumbnail(tool_item)
            self.tool_prop_layout.addRow("Template Preview:", self.lbl_template_thumb)

        elif tool_item.tool_type in ["Motion Detection", "Delete (Missing Object)"]:
            slider_thresh = QSlider(Qt.Orientation.Horizontal);
            slider_thresh.setRange(1, 255);
            slider_thresh.setValue(tool_item.threshold)
            slider_thresh.valueChanged.connect(lambda v, i=tool_item: self.update_tool_val(i, 'threshold', v))
            self.tool_prop_layout.addRow("Pixel Diff Thresh:", slider_thresh)

            slider_area = QSlider(Qt.Orientation.Horizontal);
            slider_area.setRange(10, 5000);
            slider_area.setValue(tool_item.min_area)
            slider_area.valueChanged.connect(lambda v, i=tool_item: self.update_tool_val(i, 'min_area', v))
            self.tool_prop_layout.addRow("Min Motion Area:", slider_area)

            btn_ref = QPushButton("📷 Capture Reference State")
            btn_ref.setStyleSheet("background-color: #c0392b; font-weight: bold;")
            btn_ref.clicked.connect(lambda: self.set_motion_reference(tool_item))
            self.tool_prop_layout.addRow(btn_ref)

            status = QLabel("Ready" if tool_item.reference_frame is not None else "Missing Reference Frame")
            self.tool_prop_layout.addRow("Status:", status)

        elif tool_item.tool_type == "LLM Vision":
            txt_prompt = QTextEdit()
            txt_prompt.setPlainText(tool_item.llm_prompt)
            txt_prompt.textChanged.connect(lambda i=tool_item, t=txt_prompt: setattr(i, 'llm_prompt', t.toPlainText()))
            self.tool_prop_layout.addRow("LLM Prompt:", txt_prompt)

            btn_send = QPushButton("🧠 Trigger LLM Query")
            btn_send.setStyleSheet("background-color: #9b59b6; font-weight: bold;")
            btn_send.clicked.connect(lambda: self.trigger_llm_tool(tool_item))
            self.tool_prop_layout.addRow(btn_send)

            txt_resp = QTextEdit()
            txt_resp.setReadOnly(True)
            txt_resp.setPlainText(tool_item.llm_response)
            self.llm_resp_box = txt_resp
            self.tool_prop_layout.addRow("LLM Response:", txt_resp)

        elif tool_item.tool_type == "Blob Detection":
            cb_color = QComboBox();
            cb_color.addItems(["Dark Blobs (0)", "Light Blobs (255)"])
            cb_color.setCurrentIndex(0 if tool_item.blob_color == 0 else 1)
            cb_color.currentIndexChanged.connect(
                lambda idx, i=tool_item: self.update_tool_val(i, 'blob_color', 0 if idx == 0 else 255))
            self.tool_prop_layout.addRow("Target Color:", cb_color)

            slider_min_area = QSlider(Qt.Orientation.Horizontal);
            slider_min_area.setRange(1, 5000);
            slider_min_area.setValue(tool_item.threshold)
            slider_min_area.valueChanged.connect(lambda v, i=tool_item: self.update_tool_val(i, 'threshold', v))
            self.tool_prop_layout.addRow("Min Area:", slider_min_area)

            slider_max_area = QSlider(Qt.Orientation.Horizontal);
            slider_max_area.setRange(100, 20000);
            slider_max_area.setValue(tool_item.blob_max_area)
            slider_max_area.valueChanged.connect(lambda v, i=tool_item: self.update_tool_val(i, 'blob_max_area', v))
            self.tool_prop_layout.addRow("Max Area:", slider_max_area)

            self.blob_coord_label = QLabel(
                f"X: {tool_item.last_blob_x} px | Y: {tool_item.last_blob_y} px\nAngle: {tool_item.last_blob_a:.1f}°")
            self.blob_coord_label.setStyleSheet("color: #2ecc71; font-weight: bold;")
            self.tool_prop_layout.addRow("From Origin (px):", self.blob_coord_label)

            self.tool_prop_layout.addRow(QLabel("<i>To move origin, drag the green crosshair inside the ROI.</i>"))

    def trigger_llm_tool(self, tool_item):
        if self.current_raw_frame is None or tool_item.llm_is_processing: return
        x1, y1 = int(max(0, tool_item.pos().x())), int(max(0, tool_item.pos().y()))
        x2, y2 = int(min(self.current_raw_frame.shape[1], x1 + tool_item.rect().width())), int(
            min(self.current_raw_frame.shape[0], y1 + tool_item.rect().height()))
        if x2 <= x1 or y2 <= y1: return

        crop = self.current_raw_frame[y1:y2, x1:x2]
        b64 = img_to_b64(crop)

        tool_item.llm_is_processing = True
        tool_item.llm_response = "Processing image..."
        if hasattr(self, 'llm_resp_box'): self.llm_resp_box.setPlainText(tool_item.llm_response)

        self.llm_worker = LLMWorker(tool_item.llm_prompt, b64)
        self.llm_worker.finished.connect(lambda resp, i=tool_item: self.handle_llm_result(i, resp))
        self.llm_worker.start()

    def handle_llm_result(self, tool_item, response):
        tool_item.llm_is_processing = False
        tool_item.llm_response = response
        tool_item.current_score = 100  # Trigger success
        if hasattr(self, 'llm_resp_box'): self.llm_resp_box.setPlainText(tool_item.llm_response)

    def calculate_homography(self, tool_item):
        pts_src = np.array([[p.x() + tool_item.pos().x(), p.y() + tool_item.pos().y()] for p in tool_item.points],
                           dtype="float32")
        # Averaging lengths to create a mapped rectangle
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
                    # Crash-proof manual contour mapping
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
                            # Center in ROI coordinates
                            cx = int(M["m10"] / M["m00"])
                            cy = int(M["m01"] / M["m00"])

                            # Calculate origin relative offset
                            if item.origin_handle:
                                ox, oy = item.origin_handle.pos().x(), item.origin_handle.pos().y()
                                # Convert Cartesian: origin -> right is +X, origin -> up is +Y
                                item.last_blob_x = int(cx - ox)
                                item.last_blob_y = int(oy - cy)

                                # Angle via minAreaRect
                            rect = cv2.minAreaRect(best_c)
                            item.last_blob_a = rect[2]

                            # Draw global
                            gx, gy = x1 + cx, y1 + cy
                            cv2.drawMarker(display_frame, (gx, gy), (0, 255, 0), cv2.MARKER_CROSS, 20, 2)
                            box = cv2.boxPoints(rect)
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

    def toggle_camera(self):
        if self.timer.isActive():
            self.timer.stop();
            self.camera.release();
            self.video_frame_item.setPixmap(QPixmap())
            self.btn_toggle_cam.setText("Start Camera Feed")
        else:
            self.camera_index = self.camera_selector.currentData()
            self.camera = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
            self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280);
            self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            if self.camera.isOpened(): self.timer.start(30); self.btn_toggle_cam.setText("Stop Camera Feed")

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


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_THEME)
    observatory = ObservatoryEngine(logger=print)
    observatory.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()