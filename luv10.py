import sys
import os
import time
import xml.etree.ElementTree as ET

# Silence OpenCV C++ probing errors and prevent Windows MSMF camera lockups
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
                             QGraphicsItem, QColorDialog)
from PyQt6.QtCore import pyqtSignal, QTimer, Qt, QRectF, QPointF, QRect
from PyQt6.QtGui import QColor, QBrush, QImage, QPixmap, QAction, QFont, QPen, QPainter

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
QMenuBar::item:selected { background-color: #5C6062; }
QGroupBox { border: 1px solid #555555; margin-top: 10px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
"""


# ---------------------------------------------------------
# 1. CUSTOM PROJECTOR CANVAS ASSETS
# ---------------------------------------------------------
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


class InteractiveImageItem(QGraphicsRectItem):
    def __init__(self, filepath="Placeholder"):
        super().__init__(0, 0, 200, 150)
        self.filepath = filepath
        self.setPen(QPen(QColor("#00FF00"), 2))
        self.setBrush(QBrush(QColor(40, 40, 40, 200)))
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)

        self.text_tag = QGraphicsTextItem(os.path.basename(filepath), self)
        self.text_tag.setDefaultTextColor(QColor("white"))
        self.text_tag.setPos(5, 5)

        self.is_blinking = False
        self.tied_variable = "None"
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
class ROIResizeHandle(QGraphicsRectItem):
    def __init__(self, parent, color_string):
        super().__init__(0, 0, 12, 12, parent)
        self.setBrush(QBrush(QColor(color_string)))
        self.setPen(QPen(QColor("white"), 1))
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._dragging = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            pos_in_parent = self.parentItem().mapFromItem(self, event.pos())
            new_w = max(30, pos_in_parent.x())
            new_h = max(30, pos_in_parent.y())
            self.parentItem().update_size(new_w, new_h)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._dragging = False
        event.accept()


class PatternROI(QGraphicsRectItem):
    def __init__(self, parent_search_roi):
        super().__init__(0, 0, 100, 100, parent_search_roi)
        self.setPos(50, 50)
        self.setPen(QPen(QColor("red"), 2))
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        self.handle = ROIResizeHandle(self, "red")
        self.update_handle_pos()

    def update_size(self, w, h):
        if self.parentItem():
            parent_w = self.parentItem().rect().width()
            parent_h = self.parentItem().rect().height()
            max_w = parent_w - self.pos().x()
            max_h = parent_h - self.pos().y()
            w = max(20, min(w, max_w))
            h = max(20, min(h, max_h))
            self.setRect(0, 0, w, h)
            self.update_handle_pos()

    def update_handle_pos(self):
        r = self.rect()
        self.handle.setPos(r.width() - 6, r.height() - 6)

    def itemChange(self, change, value):
        try:
            if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.parentItem():
                new_pos = value
                parent_w = self.parentItem().rect().width()
                parent_h = self.parentItem().rect().height()
                r = self.rect()
                x = max(0, min(new_pos.x(), parent_w - r.width()))
                y = max(0, min(new_pos.y(), parent_h - r.height()))
                return QPointF(x, y)
        except Exception:
            pass
        return super().itemChange(change, value)

    def validate_constraints(self):
        if self.parentItem():
            parent_w = self.parentItem().rect().width()
            parent_h = self.parentItem().rect().height()
            r = self.rect()
            w = min(r.width(), parent_w)
            h = min(r.height(), parent_h)
            self.setRect(0, 0, w, h)
            x = max(0, min(self.pos().x(), parent_w - w))
            y = max(0, min(self.pos().y(), parent_h - h))
            self.setPos(x, y)
            self.update_handle_pos()


class SearchROI(QGraphicsRectItem):
    def __init__(self, x, y, w=300, h=300, tool_name="tool01"):
        super().__init__(0, 0, w, h)
        self.tool_name = tool_name
        self.setPos(x, y)
        self.setPen(QPen(QColor("black"), 2, Qt.PenStyle.DashLine))
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        self.handle = ROIResizeHandle(self, "black")
        self.update_handle_pos()
        self.pattern_roi = PatternROI(self)

    def update_size(self, w, h):
        self.setRect(0, 0, w, h)
        self.update_handle_pos()
        self.pattern_roi.validate_constraints()

    def update_handle_pos(self):
        r = self.rect()
        self.handle.setPos(r.width() - 6, r.height() - 6)


class VisionCanvasView(QGraphicsView):
    double_clicked = pyqtSignal(float, float)

    def __init__(self, scene):
        super().__init__(scene)

    def mouseDoubleClickEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        self.double_clicked.emit(scene_pos.x(), scene_pos.y())
        super().mouseDoubleClickEvent(event)


# Custom Widget for the Visual Target Threshold Overlay
class ThresholdScoreBar(QWidget):
    trigger_state_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(40)
        self.score = 0
        self.threshold = 50
        self._dragging = False
        self.is_triggered = False

    def set_score(self, val):
        self.score = max(0, min(100, val))

        # Trigger Condition Evaluation (Logic specific to request)
        was_triggered = self.is_triggered
        self.is_triggered = (self.score >= self.threshold)

        if self.is_triggered != was_triggered:
            self.trigger_state_changed.emit(self.is_triggered)

        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        # Background Track
        painter.fillRect(rect, QColor("#1a1a1a"))

        # Score Fill Bar (Green if < Threshold, Red if >= Threshold per instructions)
        fill_width = int((self.score / 100.0) * rect.width())
        fill_rect = QRect(0, 0, fill_width, rect.height())

        if self.score < self.threshold:
            painter.fillRect(fill_rect, QColor(0, 200, 0, 180))  # Green Mode
        else:
            painter.fillRect(fill_rect, QColor(220, 0, 0, 180))  # Red Mode

        # Draggable Threshold Marker
        t_x = int((self.threshold / 100.0) * rect.width())
        painter.setPen(QPen(QColor("white"), 3))
        painter.drawLine(t_x, 0, t_x, rect.height())

        # Text Overlay
        painter.setPen(QColor("white"))
        font = QFont("Arial", 10, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                         f"Match: {self.score}%  |  Trigger Line: {self.threshold}%")

    def mousePressEvent(self, event):
        self.update_threshold_from_mouse(event.pos().x())
        self._dragging = True

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.update_threshold_from_mouse(event.pos().x())

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def update_threshold_from_mouse(self, x):
        val = int((x / self.width()) * 100)
        self.threshold = max(0, min(100, val))
        self.update()


class ObservatoryEngine(QMainWindow):
    def __init__(self, logger):
        super().__init__()
        self.logger = logger
        self.setWindowTitle("Observatory - Vision Environment")
        self.setGeometry(150, 150, 1250, 750)

        self.camera = None
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.tool_count = 0
        self.current_raw_frame = None

        self.trained_template = None
        self.template_w = 0
        self.template_h = 0

        self.tool_states = {"None": True, "tool01": False, "tool02": False, "tool03": False, "tool04": False}
        self.prev_tool_states = self.tool_states.copy()
        self.edge_filter_active = False

        self.setup_ui()

    def setup_ui(self):
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")
        file_menu.addAction(QAction("Save Config", self, triggered=self.save_observatory_config))
        file_menu.addAction(QAction("Load Config", self, triggered=self.load_observatory_config))

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        toolbar = QHBoxLayout()

        train_layout = QVBoxLayout()
        self.btn_train = QPushButton("🎯 Train Pattern")
        self.btn_train.clicked.connect(self.train_pattern)
        train_layout.addWidget(self.btn_train)

        self.train_thumbnail = QLabel("No Trained Image")
        self.train_thumbnail.setFixedSize(110, 65)
        self.train_thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.train_thumbnail.setStyleSheet(
            "background-color: #1e1e1e; border: 1px dashed #555; color: #666; font-size: 10px;")
        train_layout.addWidget(self.train_thumbnail)
        toolbar.addLayout(train_layout)

        toolbar.addStretch()

        toolbar.addWidget(QLabel("Devices: "))
        self.camera_selector = QComboBox()
        self.populate_cameras()
        toolbar.addWidget(self.camera_selector)

        self.btn_toggle_cam = QPushButton("Start Camera")
        self.btn_toggle_cam.clicked.connect(self.toggle_camera)
        toolbar.addWidget(self.btn_toggle_cam)
        main_layout.addLayout(toolbar)

        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(QLabel("<b>Vision Tools</b>"))

        self.tool_selector = QComboBox()
        self.tool_selector.addItems(["Pattern Match", "Motion Detection"])
        left_layout.addWidget(self.tool_selector)

        left_layout.addWidget(QLabel("Active ROI Tools List:"))
        self.active_tools_list = QListWidget()
        left_layout.addWidget(self.active_tools_list)

        # New Deletion Control
        self.btn_delete_tool = QPushButton("🗑️ Delete Selected Tool")
        self.btn_delete_tool.clicked.connect(self.delete_selected_tool)
        left_layout.addWidget(self.btn_delete_tool)

        h_splitter.addWidget(left_panel)

        self.cam_scene = QGraphicsScene()
        self.cam_view = VisionCanvasView(self.cam_scene)
        self.cam_view.setStyleSheet("background-color: #1e1e1e; border: 1px solid #555;")
        self.cam_view.double_clicked.connect(self.spawn_tool_roi)

        self.video_frame_item = self.cam_scene.addPixmap(QPixmap())
        h_splitter.addWidget(self.cam_view)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.addWidget(QLabel("<b>Filters & Variables</b>"))

        self.filter_selector = QComboBox()
        self.filter_selector.addItems(["None", "Edge Detection", "Depth", "Threshold"])
        self.filter_selector.currentTextChanged.connect(self.handle_filter_change)
        right_layout.addWidget(QLabel("Apply Filter Type:"))
        right_layout.addWidget(self.filter_selector)

        var_group = QGroupBox("Tool Parameters")
        var_form = QFormLayout(var_group)

        self.slider_angle = QSlider(Qt.Orientation.Horizontal)
        self.slider_angle.setRange(0, 360)
        self.slider_angle.setValue(180)
        var_form.addRow("Angular Threshold:", self.slider_angle)

        self.slider_thresh = QSlider(Qt.Orientation.Horizontal)
        self.slider_thresh.setRange(1, 255)
        self.slider_thresh.setValue(127)
        var_form.addRow("Filter Intensity:", self.slider_thresh)
        right_layout.addWidget(var_group)

        score_group = QGroupBox("Dynamic Tool Trigger Bar")
        score_layout = QVBoxLayout(score_group)

        # Implement the Visual Score Overlay Module
        self.score_bar = ThresholdScoreBar()
        score_layout.addWidget(self.score_bar)

        right_layout.addWidget(score_group)

        right_layout.addStretch()
        h_splitter.addWidget(right_panel)

        h_splitter.setSizes([220, 730, 300])
        main_layout.addWidget(h_splitter)

    def handle_filter_change(self, text):
        self.edge_filter_active = (text == "Edge Detection")

    def populate_cameras(self):
        self.camera_selector.clear()
        for i in range(4):
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                self.camera_selector.addItem(f"Camera {i} (Standard)", (i, "STANDARD"))
                cap.release()
                continue
            cap.release()

            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                self.camera_selector.addItem(f"Camera {i} (DirectShow)", (i, "DSHOW"))
                cap.release()

        if self.camera_selector.count() == 0:
            self.camera_selector.addItem("No Cameras Found", (0, "NONE"))

    def toggle_camera(self):
        if self.timer.isActive():
            self.timer.stop()
            if self.camera:
                self.camera.release()
            self.video_frame_item.setPixmap(QPixmap())
            self.btn_toggle_cam.setText("Start Camera")
        else:
            combo_data = self.camera_selector.currentData()
            if combo_data and combo_data[1] != "NONE":
                cam_idx, backend = combo_data
                if backend == "DSHOW":
                    self.camera = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW)
                else:
                    self.camera = cv2.VideoCapture(cam_idx)
                self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                if self.camera.isOpened():
                    self.timer.start(30)
                    self.btn_toggle_cam.setText("Stop Camera")

    def delete_selected_tool(self):
        curr_row = self.active_tools_list.currentRow()
        if curr_row >= 0:
            item_text = self.active_tools_list.item(curr_row).text()
            tool_name = item_text.split(" ")[0]  # Extracts "tool01"

            # Destroy the graphic objects from the camera scene matrix
            for item in list(self.cam_scene.items()):
                if isinstance(item, SearchROI) and item.tool_name == tool_name:
                    self.cam_scene.removeItem(item)

            self.active_tools_list.takeItem(curr_row)
            self.tool_states[tool_name] = False
            self.logger(f"Observatory: Deleted tool asset {tool_name}")

    def update_frame(self):
        if self.camera and self.camera.isOpened():
            try:
                ret, frame = self.camera.read()
                if ret:
                    self.current_raw_frame = frame.copy()

                    search_roi = None
                    for item in self.cam_scene.items():
                        if isinstance(item, SearchROI):
                            search_roi = item
                            break

                    if search_roi and self.edge_filter_active:
                        pos = search_roi.pos()
                        rect = search_roi.rect()
                        x1 = int(max(0, pos.x()))
                        y1 = int(max(0, pos.y()))
                        x2 = int(min(frame.shape[1], x1 + rect.width()))
                        y2 = int(min(frame.shape[0], y1 + rect.height()))

                        if x2 > x1 and y2 > y1:
                            roi_crop = frame[y1:y2, x1:x2]
                            gray = cv2.cvtColor(roi_crop, cv2.COLOR_BGR2GRAY)
                            thresh_val = self.slider_thresh.value()
                            edges = cv2.Canny(gray, thresh_val, thresh_val * 2)
                            frame[y1:y2, x1:x2] = cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR)

                    self.prev_tool_states = self.tool_states.copy()
                    if self.trained_template is not None and search_roi:
                        pos = search_roi.pos()
                        rect = search_roi.rect()
                        x1 = int(max(0, pos.x()))
                        y1 = int(max(0, pos.y()))
                        x2 = int(min(frame.shape[1], x1 + rect.width()))
                        y2 = int(min(frame.shape[0], y1 + rect.height()))

                        if (x2 - x1) >= self.template_w and (y2 - y1) >= self.template_h:
                            search_zone = frame[y1:y2, x1:x2]
                            gray_search = cv2.cvtColor(search_zone, cv2.COLOR_BGR2GRAY)

                            match_matrix = cv2.matchTemplate(gray_search, self.trained_template, cv2.TM_CCOEFF_NORMED)
                            _, max_val, _, max_loc = cv2.minMaxLoc(match_matrix)

                            score_pct = max(0, min(100, int(max_val * 100)))

                            # Pipe real-time evaluation logic to the UI Overlay
                            self.score_bar.set_score(score_pct)

                            if self.score_bar.is_triggered:
                                self.tool_states[search_roi.tool_name] = True
                                tx = x1 + max_loc[0]
                                ty = y1 + max_loc[1]
                                cv2.rectangle(frame, (tx, ty), (tx + self.template_w, ty + self.template_h),
                                              (0, 255, 0), 3)
                            else:
                                self.tool_states[search_roi.tool_name] = False

                    rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    h, w, ch = rgb_image.shape
                    qt_image = QImage(rgb_image.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()
                    self.video_frame_item.setPixmap(QPixmap.fromImage(qt_image))

                    if self.cam_scene.sceneRect().width() != w:
                        self.cam_scene.setSceneRect(0, 0, w, h)
                        self.cam_view.fitInView(self.cam_scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            except Exception as e:
                print(f"Observatory Runtime Stream Error: {e}")

    def spawn_tool_roi(self, x, y):
        selected_tool = self.tool_selector.currentText()
        self.tool_count += 1
        tool_name = f"tool{self.tool_count:02d}"
        self.active_tools_list.addItem(f"{tool_name} ({selected_tool})")

        if selected_tool == "Pattern Match":
            roi = SearchROI(x - 150, y - 150, tool_name=tool_name)
            self.cam_scene.addItem(roi)

    def train_pattern(self):
        try:
            search_roi = None
            for item in self.cam_scene.items():
                if isinstance(item, SearchROI):
                    search_roi = item
                    break

            if search_roi and self.current_raw_frame is not None:
                p_roi = search_roi.pattern_roi
                global_x = int(search_roi.pos().x() + p_roi.pos().x())
                global_y = int(search_roi.pos().y() + p_roi.pos().y())
                w = int(p_roi.rect().width())
                h = int(p_roi.rect().height())

                frame_to_crop = self.current_raw_frame.copy()
                if self.edge_filter_active:
                    sx = int(max(0, search_roi.pos().x()))
                    sy = int(max(0, search_roi.pos().y()))
                    ex = int(min(frame_to_crop.shape[1], sx + search_roi.rect().width()))
                    ey = int(min(frame_to_crop.shape[0], sy + search_roi.rect().height()))
                    if ex > sx and ey > sy:
                        roi_crop = frame_to_crop[sy:ey, sx:ex]
                        gray = cv2.cvtColor(roi_crop, cv2.COLOR_BGR2GRAY)
                        t_val = self.slider_thresh.value()
                        edges = cv2.Canny(gray, t_val, t_val * 2)
                        frame_to_crop[sy:ey, sx:ex] = cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR)

                x1 = max(0, global_x)
                y1 = max(0, global_y)
                x2 = min(frame_to_crop.shape[1], x1 + w)
                y2 = min(frame_to_crop.shape[0], y1 + h)

                if x2 > x1 and y2 > y1:
                    train_crop = frame_to_crop[y1:y2, x1:x2]
                    self.trained_template = cv2.cvtColor(train_crop, cv2.COLOR_BGR2GRAY)
                    self.template_w = w
                    self.template_h = h

                    rgb_crop = cv2.cvtColor(train_crop, cv2.COLOR_BGR2RGB)
                    ch, cw, cd = rgb_crop.shape
                    qt_crop = QImage(rgb_crop.data, cw, ch, cd * cw, QImage.Format.Format_RGB888).copy()
                    pixmap = QPixmap.fromImage(qt_crop).scaled(
                        self.train_thumbnail.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation
                    )
                    self.train_thumbnail.setPixmap(pixmap)
        except Exception as e:
            self.logger(f"Observatory Training Error: {str(e)}")

    def save_observatory_config(self):
        filepath, _ = QFileDialog.getSaveFileName(self, "Save Observatory XML", "", "XML Files (*.xml)")
        if not filepath: return
        root = ET.Element("ObservatoryConfig")
        ET.SubElement(root, "Filter").text = self.filter_selector.currentText()
        ET.SubElement(root, "Sliders", angle=str(self.slider_angle.value()), intensity=str(self.slider_thresh.value()),
                      score=str(self.score_bar.threshold))
        tree = ET.ElementTree(root)
        if hasattr(ET, 'indent'): ET.indent(tree, space="    ", level=0)
        tree.write(filepath, encoding="utf-8", xml_declaration=True)

    def load_observatory_config(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Open Observatory XML", "", "XML Files (*.xml)")
        if not filepath: return
        try:
            tree = ET.parse(filepath)
            root = tree.getroot()
            f_node = root.find("Filter")
            if f_node is not None: self.filter_selector.setCurrentText(f_node.text or "None")
            # Restore Threshold
            s_node = root.find("Sliders")
            if s_node is not None:
                self.score_bar.threshold = int(s_node.get("score", 50))
                self.score_bar.update()
        except Exception as e:
            print(e)

    def closeEvent(self, event):
        if self.camera:
            self.camera.release()
        event.accept()


# ---------------------------------------------------------
# 4. THE AUTHORING STUDIO ENVIRONMENT (Execution Workspace)
# ---------------------------------------------------------
class StudioCanvasView(QGraphicsView):
    file_dropped = pyqtSignal(str)

    def __init__(self, scene, parent_interface):
        super().__init__(scene)
        self.parent_interface = parent_interface
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                filepath = url.toLocalFile()
                self.file_dropped.emit(filepath)
            event.acceptProposedAction()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_V and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            clipboard = QApplication.clipboard()
            mime_data = clipboard.mimeData()
            if mime_data.hasUrls():
                for url in mime_data.urls():
                    self.file_dropped.emit(url.toLocalFile())
            else:
                self.parent_interface.insert_text_asset()
        else:
            super().keyPressEvent(event)


class AuthoringInterface(QMainWindow):
    def __init__(self, canvas):
        super().__init__()
        self.setWindowTitle("LightGuide Studio - Authoring Environment")
        self.setGeometry(200, 200, 1500, 850)

        self.canvas = canvas
        self.studio_scene = self.canvas.scene
        self.current_file = None
        self.active_item = None
        self.active_item_timers = {}

        self.setup_ui()
        self.observatory = ObservatoryEngine(self.log_message)

        self.studio_scene.selectionChanged.connect(self.handle_canvas_selection)

        self.blink_state = True
        self.master_clock = QTimer()
        self.master_clock.timeout.connect(self.process_system_cycle)
        self.master_clock.start(500)

        self.log_message("Authoring System Ready.")

    def setup_ui(self):
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")
        file_menu.addAction(QAction("New Sequence Layout", self, triggered=self.new_file))
        file_menu.addAction(QAction("Open Workspace Sequence...", self, triggered=self.open_file))
        file_menu.addAction(QAction("Save Workspace Sequence", self, triggered=self.save_file))

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        top_properties_box = QGroupBox("Global Step Execution Properties Configuration")
        top_prop_layout = QHBoxLayout(top_properties_box)

        top_prop_layout.addWidget(QLabel("End Function Action: "))
        self.global_end_function = QComboBox()
        self.global_end_function.addItems(["End Program Execution", "Loop Continuously", "Jump to Step Line"])
        top_prop_layout.addWidget(self.global_end_function)

        top_prop_layout.addWidget(QLabel("Jump Step Target: "))
        self.global_jump_target = QSpinBox()
        self.global_jump_target.setRange(1, 100)
        top_prop_layout.addWidget(self.global_jump_target)

        self.btn_clear_all = QPushButton("🗑️ Clear Canvas Work")
        self.btn_clear_all.clicked.connect(self.clear_canvas_and_timeline)
        top_prop_layout.addWidget(self.btn_clear_all)

        top_prop_layout.addStretch()

        btn_obs = QPushButton("👁️ Launch Observatory Vision")
        btn_obs.clicked.connect(lambda: self.observatory.show())
        top_prop_layout.addWidget(btn_obs)
        main_layout.addWidget(top_properties_box)

        v_splitter = QSplitter(Qt.Orientation.Vertical)
        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(QLabel("<b>Sequence Injector</b>"))

        self.btn_add_step = QPushButton("🟦 Add Step Group")
        self.btn_add_step.clicked.connect(self.add_new_step_node)
        self.btn_add_step.setStyleSheet("background-color: #2f65ca;")
        left_layout.addWidget(self.btn_add_step)

        self.btn_insert_text = QPushButton("+ Insert Text Element")
        self.btn_insert_text.clicked.connect(self.insert_text_asset)
        left_layout.addWidget(self.btn_insert_text)

        self.btn_insert_image = QPushButton("+ Insert Image Asset")
        self.btn_insert_image.clicked.connect(self.browse_image_asset)
        left_layout.addWidget(self.btn_insert_image)

        left_layout.addStretch()
        self.btn_delete_node = QPushButton("🗑️ Delete Selected Node")
        self.btn_delete_node.clicked.connect(self.delete_selected_tree_node)
        left_layout.addWidget(self.btn_delete_node)

        h_splitter.addWidget(left_panel)

        # Replacing Table with the Hierarchical Tree Widget View requested in reference image
        center_panel = QWidget()
        center_layout = QVBoxLayout(center_panel)
        center_layout.addWidget(QLabel("<b>Sequence Execution Steps (Hierarchy)</b>"))

        self.step_tree = QTreeWidget()
        self.step_tree.setHeaderLabels(
            ["Sequence Pipeline Structure", "Action Parameters / Trigger", "Properties Matrix"])
        self.step_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.step_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.step_tree.setAlternatingRowColors(True)
        self.step_tree.itemDoubleClicked.connect(self.handle_tree_item_double_click)
        center_layout.addWidget(self.step_tree)
        h_splitter.addWidget(center_panel)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.addWidget(QLabel("<b>Asset Node Property Inspector</b>"))

        self.prop_group = QGroupBox("Properties Mapping Matrix")
        prop_form = QFormLayout(self.prop_group)

        self.prop_text = QLineEdit()
        self.prop_text.textChanged.connect(self.update_active_item)

        self.prop_font = QFontComboBox()
        self.prop_font.currentFontChanged.connect(self.update_active_item)

        self.prop_size = QSpinBox()
        self.prop_size.setRange(8, 300)
        self.prop_size.valueChanged.connect(self.update_active_item)

        self.prop_rotation = QSpinBox()
        self.prop_rotation.setRange(0, 359)
        self.prop_rotation.valueChanged.connect(self.update_active_item)

        self.btn_color_pick = QPushButton("Select Color Spectrum")
        self.btn_color_pick.clicked.connect(self.pick_text_color)

        self.prop_blink = QCheckBox("Enable Cyclic Blinking Mode")
        self.prop_blink.stateChanged.connect(self.update_active_item)

        self.prop_var = QComboBox()
        self.prop_var.addItems(["None", "tool01", "tool02", "tool03", "tool04"])
        self.prop_var.currentTextChanged.connect(self.update_active_item)

        self.prop_condition = QComboBox()
        self.prop_condition.addItems(["On True", "False to True", "True to False", "On False"])
        self.prop_condition.currentTextChanged.connect(self.update_active_item)

        self.prop_duration = QSpinBox()
        self.prop_duration.setRange(1, 3600)
        self.prop_duration.setValue(10)
        self.prop_duration.valueChanged.connect(self.update_active_item)

        prop_form.addRow("Text Raw Context:", self.prop_text)
        prop_form.addRow("Font Layout Matrix:", self.prop_font)
        prop_form.addRow("Point Sizing Suffix:", self.prop_size)
        prop_form.addRow("Rotational Degrees:", self.prop_rotation)
        prop_form.addRow("Color Hex:", self.btn_color_pick)
        prop_form.addRow("Blink Profiler:", self.prop_blink)
        prop_form.addRow("Observatory Variable Binding:", self.prop_var)
        prop_form.addRow("Boundary Condition State:", self.prop_condition)
        prop_form.addRow("Duration (Seconds):", self.prop_duration)

        self.prop_group.setEnabled(False)
        right_layout.addWidget(self.prop_group)
        right_layout.addStretch()
        h_splitter.addWidget(right_panel)

        h_splitter.setSizes([200, 950, 350])

        self.console = QTextBrowser()
        self.console.setReadOnly(True)

        v_splitter.addWidget(h_splitter)
        v_splitter.addWidget(self.console)
        v_splitter.setSizes([680, 120])
        main_layout.addWidget(v_splitter)

        self.canvas.view.setAcceptDrops(True)
        self.studio_override_view = StudioCanvasView(self.studio_scene, self)
        self.studio_override_view.file_dropped.connect(self.insert_media_via_path)
        self.canvas.setCentralWidget(self.studio_override_view)

        # Initialize Default Entry state
        self.add_new_step_node()

    def clear_canvas_and_timeline(self):
        self.canvas.clear_canvas()
        self.step_tree.clear()
        self.active_item_timers.clear()
        self.add_new_step_node()
        self.log_message("Authoring Interface: Active workspace cleared.")

    def add_new_step_node(self):
        step_idx = self.step_tree.topLevelItemCount() + 1
        item = QTreeWidgetItem(self.step_tree, [f"Step Start [{step_idx}]"])
        item.setBackground(0, QColor("#1e508c"))  # Blue header from reference
        item.setForeground(0, QColor("white"))
        font = QFont()
        font.setBold(True)
        item.setFont(0, font)

        # Add default Clear Action
        clear_item = QTreeWidgetItem(item, ["🧹 Clear Canvas [All]", "", ""])
        item.setExpanded(True)
        self.step_tree.setCurrentItem(item)

    def delete_selected_tree_node(self):
        selected = self.step_tree.selectedItems()
        if not selected: return
        item = selected[0]

        # Determine if it has a linked graphic item
        asset = item.data(0, Qt.ItemDataRole.UserRole)
        if asset and isinstance(asset, QGraphicsItem):
            self.studio_scene.removeItem(asset)

        parent = item.parent()
        if parent:
            parent.removeChild(item)
        else:
            # Top level item delete
            root = self.step_tree.invisibleRootItem()
            root.removeChild(item)

    def handle_tree_item_double_click(self, item, column):
        asset = item.data(0, Qt.ItemDataRole.UserRole)
        if asset and isinstance(asset, QGraphicsItem):
            self.studio_scene.clearSelection()
            asset.setSelected(True)
            self.studio_override_view.centerOn(asset)
            self.log_message("Authoring Interface: Focused sequence element.")

    def insert_text_asset(self):
        parent_step = self.get_active_step_parent()
        text_item = InteractiveTextItem("New Text Line Input")
        text_item.setPos(200, 200)
        self.studio_scene.addItem(text_item)

        tree_item = QTreeWidgetItem(parent_step, ["✏️ Text Element", "Wait For [None]", "Dur: 10s"])
        tree_item.setData(0, Qt.ItemDataRole.UserRole, text_item)  # Embed pointer reference into list row
        self.log_message("Authoring Interface: Injected text structure block.")

    def browse_image_asset(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Select Image or Video Matrix Asset", "",
                                                  "Media Files (*.png *.jpg *.mp4 *.avi)")
        if filepath:
            self.insert_media_via_path(filepath)

    def insert_media_via_path(self, filepath):
        parent_step = self.get_active_step_parent()
        img_item = InteractiveImageItem(filepath)
        img_item.setPos(250, 250)
        self.studio_scene.addItem(img_item)

        tree_item = QTreeWidgetItem(parent_step, [f"🎨 {os.path.basename(filepath)}", "Wait For [None]", "Dur: 10s"])
        tree_item.setData(0, Qt.ItemDataRole.UserRole, img_item)
        self.log_message(f"Authoring Interface: Loaded resource target {os.path.basename(filepath)}")

    def get_active_step_parent(self):
        selected = self.step_tree.selectedItems()
        if not selected:
            # Revert to last top level step
            count = self.step_tree.topLevelItemCount()
            if count > 0:
                return self.step_tree.topLevelItem(count - 1)
            else:
                self.add_new_step_node()
                return self.step_tree.topLevelItem(0)

        node = selected[0]
        if node.parent(): return node.parent()
        return node

    def pick_text_color(self):
        if isinstance(self.active_item, InteractiveTextItem):
            color = QColorDialog.getColor(self.active_item.defaultTextColor(), self,
                                          "Select Text Rendering Matrix Color Hex")
            if color.isValid():
                self.active_item.setDefaultTextColor(color)
                self.update_tree_node_metadata(self.active_item)

    def handle_canvas_selection(self):
        selected = self.studio_scene.selectedItems()
        if not selected:
            self.active_item = None
            self.prop_group.setEnabled(False)
            return

        item = selected[0]
        if isinstance(item, (InteractiveTextItem, InteractiveImageItem)):
            self.active_item = item
            self.prop_group.setEnabled(True)

            self.prop_text.blockSignals(True)
            self.prop_font.blockSignals(True)
            self.prop_size.blockSignals(True)
            self.prop_rotation.blockSignals(True)
            self.prop_blink.blockSignals(True)
            self.prop_var.blockSignals(True)
            self.prop_condition.blockSignals(True)
            self.prop_duration.blockSignals(True)

            if isinstance(item, InteractiveTextItem):
                self.prop_text.setText(item.toPlainText())
                self.prop_font.setCurrentFont(item.font())
                self.prop_size.setValue(item.font().pointSize())
                self.prop_text.setVisible(True)
                self.prop_font.setVisible(True)
                self.prop_size.setVisible(True)
                self.btn_color_pick.setVisible(True)
            else:
                self.prop_text.setVisible(False)
                self.prop_font.setVisible(False)
                self.prop_size.setVisible(False)
                self.btn_color_pick.setVisible(False)

            self.prop_rotation.setValue(int(item.rotation()))
            self.prop_blink.setChecked(item.is_blinking)
            self.prop_var.setCurrentText(item.tied_variable)
            self.prop_condition.setCurrentText(item.trigger_condition)
            self.prop_duration.setValue(item.display_duration)

            self.prop_text.blockSignals(False)
            self.prop_font.blockSignals(False)
            self.prop_size.blockSignals(False)
            self.prop_rotation.blockSignals(False)
            self.prop_blink.blockSignals(False)
            self.prop_var.blockSignals(False)
            self.prop_condition.blockSignals(False)
            self.prop_duration.blockSignals(False)

    def update_active_item(self):
        if not self.active_item: return

        if isinstance(self.active_item, InteractiveTextItem):
            self.active_item.setPlainText(self.prop_text.text())
            new_font = self.prop_font.currentFont()
            new_font.setPointSize(self.prop_size.value())
            self.active_item.setFont(new_font)

        self.active_item.setRotation(self.prop_rotation.value())
        self.active_item.is_blinking = self.prop_blink.isChecked()
        self.active_item.tied_variable = self.prop_var.currentText()
        self.active_item.trigger_condition = self.prop_condition.currentText()
        self.active_item.display_duration = self.prop_duration.value()

        self.update_tree_node_metadata(self.active_item)

    def update_tree_node_metadata(self, canvas_item):
        """Scans tree for the embedded element pointer to update textual UI feedback."""
        iterator = QTreeWidgetItemIterator(self.step_tree)
        while iterator.value():
            node = iterator.value()
            if node.data(0, Qt.ItemDataRole.UserRole) == canvas_item:
                node.setText(1, f"Wait For [{canvas_item.tied_variable}]")
                meta = f"Dur: {canvas_item.display_duration}s | Rot: {canvas_item.rotation()}°"
                if canvas_item.is_blinking: meta += " [Blink]"
                node.setText(2, meta)
                break
            iterator += 1

    def process_system_cycle(self):
        self.blink_state = not self.blink_state

        vision_registry = self.observatory.tool_states
        prev_vision_registry = self.observatory.prev_tool_states

        for item in self.studio_scene.items():
            if isinstance(item, (InteractiveTextItem, InteractiveImageItem)):
                target_var = item.tied_variable

                if target_var in vision_registry:
                    is_now_true = vision_registry[target_var]
                    was_true = prev_vision_registry.get(target_var, False)
                    condition = item.trigger_condition

                    triggered = False
                    if condition == "On True" and is_now_true:
                        triggered = True
                    elif condition == "On False" and not is_now_true:
                        triggered = True
                    elif condition == "False to True" and (is_now_true and not was_true):
                        triggered = True
                    elif condition == "True to False" and (not is_now_true and was_true):
                        triggered = True

                    if triggered:
                        if item not in self.active_item_timers:
                            self.active_item_timers[item] = time.time() + item.display_duration

                    if item in self.active_item_timers:
                        if time.time() < self.active_item_timers[item]:
                            if item.is_blinking:
                                item.setVisible(self.blink_state)
                            else:
                                item.setVisible(True)
                        else:
                            item.setVisible(False)
                            if not triggered:
                                del self.active_item_timers[item]
                    else:
                        item.setVisible(False)
                else:
                    if item.is_blinking:
                        item.setVisible(self.blink_state)
                    else:
                        item.setVisible(True)

    def new_file(self):
        self.clear_canvas_and_timeline()
        self.current_file = None
        self.log_message("Authoring Interface: Initialized new layout sequence.")

    def save_file(self):
        filepath, _ = QFileDialog.getSaveFileName(self, "Save Production Configuration", "", "XML Files (*.xml)")
        if not filepath: return

        root = ET.Element("LightGuideDesign")

        global_config = ET.SubElement(root, "GlobalConfiguration")
        ET.SubElement(global_config, "EndAction").text = self.global_end_function.currentText()
        ET.SubElement(global_config, "JumpTargetLine").text = str(self.global_jump_target.value())

        # Serialize the Tree Layout
        for i in range(self.step_tree.topLevelItemCount()):
            top_node = self.step_tree.topLevelItem(i)
            step_elem = ET.SubElement(root, "StepBlock", label=top_node.text(0))

            for j in range(top_node.childCount()):
                child_node = top_node.child(j)
                asset = child_node.data(0, Qt.ItemDataRole.UserRole)

                if asset:
                    type_str = "InteractiveTextItem" if isinstance(asset,
                                                                   InteractiveTextItem) else "InteractiveImageItem"
                    asset_elem = ET.SubElement(step_elem, "Asset", type=type_str)

                    if isinstance(asset, InteractiveTextItem):
                        ET.SubElement(asset_elem, "ValueContext").text = asset.toPlainText()
                        ET.SubElement(asset_elem, "FontProfile", family=asset.font().family(),
                                      size=str(asset.font().pointSize()))
                        ET.SubElement(asset_elem, "ColorRGBA", r=str(asset.defaultTextColor().red()),
                                      g=str(asset.defaultTextColor().green()), b=str(asset.defaultTextColor().blue()))
                    else:
                        ET.SubElement(asset_elem, "ResourcePath").text = asset.filepath

                    ET.SubElement(asset_elem, "PositionMatrix", x=str(asset.pos().x()), y=str(asset.pos().y()))
                    ET.SubElement(asset_elem, "RotationDegrees", angle=str(asset.rotation()))
                    ET.SubElement(asset_elem, "SignalTrigger", sourceVariable=asset.tied_variable,
                                  boundaryCondition=asset.trigger_condition,
                                  temporalLifespan=str(asset.display_duration))
                    ET.SubElement(asset_elem, "BehavioralMask", blink=str(asset.is_blinking))

        tree = ET.ElementTree(root)
        if hasattr(ET, 'indent'): ET.indent(tree, space="    ", level=0)
        tree.write(filepath, encoding="utf-8", xml_declaration=True)
        self.current_file = filepath
        self.log_message(
            f"Authoring Interface: Successfully saved hierarchical XML configuration to: {self.current_file}")

    def open_file(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Open Production Configuration", "", "XML Files (*.xml)")
        if not filepath: return

        try:
            tree = ET.parse(filepath)
            root = tree.getroot()
            self.canvas.clear_canvas()
            self.step_tree.clear()

            g_node = root.find("GlobalConfiguration")
            if g_node is not None:
                ea = g_node.find("EndAction")
                if ea is not None: self.global_end_function.setCurrentText(ea.text or "End Program Execution")
                jt = g_node.find("JumpTargetLine")
                if jt is not None: self.global_jump_target.setValue(int(jt.text or 1))

            for step_block in root.findall("StepBlock"):
                top_item = QTreeWidgetItem(self.step_tree, [step_block.get("label", "Step Start")])
                top_item.setBackground(0, QColor("#1e508c"))
                top_item.setForeground(0, QColor("white"))
                font = QFont()
                font.setBold(True)
                top_item.setFont(0, font)

                QTreeWidgetItem(top_item, ["🧹 Clear Canvas [All]", "", ""])
                top_item.setExpanded(True)

                for asset in step_block.findall("Asset"):
                    a_type = asset.get("type")
                    new_item = None

                    if a_type == "InteractiveTextItem":
                        new_item = InteractiveTextItem()
                        ctx = asset.find("ValueContext")
                        if ctx is not None: new_item.setPlainText(ctx.text or "")
                        fp = asset.find("FontProfile")
                        if fp is not None: new_item.setFont(QFont(fp.get("family", "Arial"), int(fp.get("size", 48))))
                        rgba = asset.find("ColorRGBA")
                        if rgba is not None: new_item.setDefaultTextColor(
                            QColor(int(rgba.get("r")), int(rgba.get("g")), int(rgba.get("b"))))
                        display_name = "✏️ Text Element"
                    else:
                        rp = asset.find("ResourcePath")
                        path_str = rp.text if rp is not None else "Placeholder"
                        new_item = InteractiveImageItem(path_str)
                        display_name = f"🎨 {os.path.basename(path_str)}"

                    pm = asset.find("PositionMatrix")
                    if pm is not None: new_item.setPos(float(pm.get("x", 0)), float(pm.get("y", 0)))
                    rd = asset.find("RotationDegrees")
                    if rd is not None: new_item.setRotation(float(rd.get("angle", 0)))

                    st = asset.find("SignalTrigger")
                    if st is not None:
                        new_item.tied_variable = st.get("sourceVariable", "None")
                        new_item.trigger_condition = st.get("boundaryCondition", "On True")
                        new_item.display_duration = int(st.get("temporalLifespan", 10))

                    bm = asset.find("BehavioralMask")
                    if bm is not None: new_item.is_blinking = (bm.get("blink") == "True")

                    self.studio_scene.addItem(new_item)

                    child_item = QTreeWidgetItem(top_item, [display_name, f"Wait For [{new_item.tied_variable}]", ""])
                    child_item.setData(0, Qt.ItemDataRole.UserRole, new_item)
                    self.update_tree_node_metadata(new_item)

            self.active_item_timers = {}
            self.current_file = filepath
            self.log_message(f"Authoring Interface: Compiled hierarchical tree structure from: {filepath}")
        except Exception as e:
            self.log_message(f"Authoring Compilation Fatal Error: {str(e)}")

    def log_message(self, message):
        ts = time.strftime("[%H:%M:%S]")
        self.console.append(f"<span style='color:#00FF00;'>{ts} {message}</span>")


# ---------------------------------------------------------
# 5. RUNTIME INTERACTION ORCHESTRATOR
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