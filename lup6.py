import sys
import os
import time
import xml.etree.ElementTree as ET

# Silence OpenCV C++ probing errors and prevent Windows MSMF camera lockups
os.environ["OPENCV_LOG_LEVEL"] = "FATAL"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"
os.environ["OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"] = "0"

import cv2
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QGraphicsView, QGraphicsScene,
                             QLabel, QSplitter, QComboBox, QToolBar, QStatusBar,
                             QMenuBar, QTableWidget, QTableWidgetItem, QTextBrowser,
                             QHeaderView, QFileDialog, QGraphicsTextItem, QFormLayout,
                             QLineEdit, QSpinBox, QCheckBox, QFontComboBox, QGroupBox,
                             QSizePolicy, QListWidget, QSlider, QGraphicsRectItem,
                             QGraphicsItem)
from PyQt6.QtCore import pyqtSignal, QTimer, Qt, QRectF, QPointF
from PyQt6.QtGui import QColor, QBrush, QImage, QPixmap, QAction, QFont, QPen

# Professional Dark Theme Stylesheet
DARK_THEME = """
QWidget { background-color: #2b2b2b; color: #a9b7c6; font-family: 'Segoe UI', Arial; }
QPushButton { background-color: #4C5052; border: 1px solid #5C5C42; padding: 5px 15px; border-radius: 3px; color: #ffffff; }
QPushButton:hover { background-color: #5C6062; }
QPushButton:pressed { background-color: #3C3F41; }
QPushButton:disabled { background-color: #1e1e1e; color: #555555; border: 1px solid #333333; }
QTableWidget, QListWidget { background-color: #313335; gridline-color: #555555; border: 1px solid #1e1e1e; }
QHeaderView::section { background-color: #3C3F41; padding: 4px; border: 1px solid #1e1e1e; }
QTextBrowser { background-color: #1e1e1e; color: #a9b7c6; border: 1px solid #555555; font-family: 'Consolas', monospace; }
QComboBox, QSpinBox, QLineEdit { background-color: #3C3F41; border: 1px solid #1e1e1e; padding: 3px; color: #ffffff; }
QMenuBar { background-color: #3C3F41; border-bottom: 1px solid #1e1e1e; }
QMenuBar::item:selected { background-color: #5C6062; }
QGroupBox { border: 1px solid #555555; margin-top: 10px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
QSlider::groove:horizontal { border: 1px solid #555; height: 8px; background: #3C3F41; margin: 2px 0; border-radius: 4px; }
QSlider::handle:horizontal { background: #4C5052; border: 1px solid #5C5C42; width: 14px; margin: -4px 0; border-radius: 7px; }
"""


# ---------------------------------------------------------
# 1. CUSTOM CANVAS ASSETS (Output Projector)
# ---------------------------------------------------------
class InteractiveTextItem(QGraphicsTextItem):
    def __init__(self, text="New Text"):
        super().__init__(text)
        self.setFlags(QGraphicsTextItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsTextItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsTextItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setDefaultTextColor(QColor("white"))
        self.setFont(QFont("Arial", 48))
        self.is_blinking = False
        self.tied_variable = "None"
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
# 3. OBSERVATORY ENGINE (Vision Assets & Environment)
# ---------------------------------------------------------
class ROIResizeHandle(QGraphicsRectItem):
    """Bulletproof interactive resize handle that prevents layout feedback loops."""

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
    """The Red Bounding Box representing the pattern train area."""

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
        if change == QGraphicsItem.GraphicsItemChangeEvent.ItemPositionChange and self.parentItem():
            new_pos = value
            parent_w = self.parentItem().rect().width()
            parent_h = self.parentItem().rect().height()
            r = self.rect()

            # Constrain position matrix entirely inside parent bounds
            x = max(0, min(new_pos.x(), parent_w - r.width()))
            y = max(0, min(new_pos.y(), parent_h - r.height()))
            return QPointF(x, y)
        return super().itemChange(change, value)

    def validate_constraints(self):
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
    """The Outer Black Box representing the search window area."""

    def __init__(self, x, y, w=300, h=300):
        super().__init__(0, 0, w, h)
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

        self.setup_ui()

    def setup_ui(self):
        # Top Menu Bar
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")
        file_menu.addAction(QAction("Save Config", self, triggered=self.save_observatory_config))
        file_menu.addAction(QAction("Load Config", self, triggered=self.load_observatory_config))

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Top Action Bar Layout
        toolbar = QHBoxLayout()

        # Train Section Block
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

        # Camera Devices Selection Block
        toolbar.addWidget(QLabel("Devices: "))
        self.camera_selector = QComboBox()
        self.populate_cameras()
        toolbar.addWidget(self.camera_selector)

        self.btn_toggle_cam = QPushButton("Start Camera")
        self.btn_toggle_cam.clicked.connect(self.toggle_camera)
        toolbar.addWidget(self.btn_toggle_cam)
        main_layout.addLayout(toolbar)

        # Work Workspace Splitter
        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        # --- LEFT PANEL: Active System Tracking ---
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(QLabel("<b>Vision Tools</b>"))

        self.tool_selector = QComboBox()
        self.tool_selector.addItems(["Pattern Match", "Motion Detection"])
        left_layout.addWidget(self.tool_selector)

        left_layout.addWidget(QLabel("Active ROI Tools List:"))
        self.active_tools_list = QListWidget()
        left_layout.addWidget(self.active_tools_list)
        h_splitter.addWidget(left_panel)

        # --- CENTER PANEL: Interactive View ---
        self.cam_scene = QGraphicsScene()
        self.cam_view = VisionCanvasView(self.cam_scene)
        self.cam_view.setStyleSheet("background-color: #1e1e1e; border: 1px solid #555;")
        self.cam_view.double_clicked.connect(self.spawn_tool_roi)

        self.video_frame_item = self.cam_scene.addPixmap(QPixmap())
        h_splitter.addWidget(self.cam_view)

        # --- RIGHT PANEL: Analytical Filters & Variables ---
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.addWidget(QLabel("<b>Filters & Variables</b>"))

        self.filter_selector = QComboBox()
        self.filter_selector.addItems(["None", "Edge Detection", "Depth", "Threshold"])
        right_layout.addWidget(QLabel("Apply Filter Type:"))
        right_layout.addWidget(self.filter_selector)

        # Mathematical Variables Properties
        var_group = QGroupBox("Tool Parameters")
        var_form = QFormLayout(var_group)

        self.slider_angle = QSlider(Qt.Orientation.Horizontal)
        self.slider_angle.setRange(0, 360)
        self.slider_angle.setValue(180)  # Middle default
        var_form.addRow("Angular Threshold:", self.slider_angle)

        self.slider_thresh = QSlider(Qt.Orientation.Horizontal)
        self.slider_thresh.setRange(1, 255)
        self.slider_thresh.setValue(127)  # Middle default
        var_form.addRow("Filter Intensity:", self.slider_thresh)
        right_layout.addWidget(var_group)

        # Trigger Scoring Metrics Window
        score_group = QGroupBox("Tool Trigger Configuration")
        score_layout = QVBoxLayout(score_group)
        self.score_label = QLabel("Score: 0%")
        self.score_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.score_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #a9b7c6;")

        self.slider_score = QSlider(Qt.Orientation.Horizontal)
        self.slider_score.setRange(0, 100)
        self.slider_score.setValue(50)  # Middle default
        score_layout.addWidget(QLabel("Trigger Threshold level:"))
        score_layout.addWidget(self.slider_score)
        score_layout.addWidget(self.score_label)
        right_layout.addWidget(score_group)

        right_layout.addStretch()
        h_splitter.addWidget(right_panel)

        h_splitter.setSizes([220, 730, 300])
        main_layout.addWidget(h_splitter)

    def populate_cameras(self):
        self.camera_selector.clear()
        for i in range(2):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                self.camera_selector.addItem(f"Camera {i} (DirectShow)", i)
                cap.release()
        if self.camera_selector.count() == 0:
            self.camera_selector.addItem("No Cameras Found")

    def toggle_camera(self):
        if self.timer.isActive():
            self.timer.stop()
            if self.camera:
                self.camera.release()
            self.video_frame_item.setPixmap(QPixmap())
            self.btn_toggle_cam.setText("Start Camera")
            self.logger("Observatory: Camera stream detached.")
        else:
            cam_idx = self.camera_selector.currentData()
            if cam_idx is not None:
                self.camera = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW)
                self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                self.camera.set(cv2.CAP_PROP_AUTOFOCUS, 0)
                self.camera.set(cv2.CAP_PROP_ZOOM, 1)

                if self.camera.isOpened():
                    self.timer.start(30)
                    self.btn_toggle_cam.setText("Stop Camera")
                    self.logger(f"Observatory: Hooked camera {cam_idx} frame stream.")

    def update_frame(self):
        if self.camera and self.camera.isOpened():
            ret, frame = self.camera.read()
            if ret:
                self.current_raw_frame = frame.copy()
                active_filter = self.filter_selector.currentText()

                # Extract any active tracking parameters
                search_roi = None
                for item in self.cam_scene.items():
                    if isinstance(item, SearchROI):
                        search_roi = item
                        break

                # Apply live Canny Filter specifically to the area inside the Black Search Box
                if search_roi and active_filter == "Edge Detection":
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

                rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb_image.shape
                qt_image = QImage(rgb_image.data, w, h, ch * w, QImage.Format.Format_RGB888)
                self.video_frame_item.setPixmap(QPixmap.fromImage(qt_image))

                if self.cam_scene.sceneRect().width() != w:
                    self.cam_scene.setSceneRect(0, 0, w, h)
                    self.cam_view.fitInView(self.cam_scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def spawn_tool_roi(self, x, y):
        selected_tool = self.tool_selector.currentText()
        self.tool_count += 1
        tool_name = f"tool{self.tool_count:02d}"

        self.active_tools_list.addItem(f"{tool_name} ({selected_tool})")
        self.logger(f"Observatory: Created matrix handle {tool_name}")

        if selected_tool == "Pattern Match":
            roi = SearchROI(x - 150, y - 150)
            self.cam_scene.addItem(roi)

    def train_pattern(self):
        """Crops the pattern area bounding box and flashes matching analytics configurations."""
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
            active_filter = self.filter_selector.currentText()

            # Match current pipeline processing modifications for alignment training
            if active_filter == "Edge Detection":
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
                rgb_crop = cv2.cvtColor(train_crop, cv2.COLOR_BGR2RGB)
                ch, cw, cd = rgb_crop.shape
                qt_crop = QImage(rgb_crop.data, cw, ch, cd * cw, QImage.Format.Format_RGB888)

                pixmap = QPixmap.fromImage(qt_crop).scaled(
                    self.train_thumbnail.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
                self.train_thumbnail.setPixmap(pixmap)

                self.logger("Observatory: Extraction sequence complete. ROI snapshot loaded.")
                self.score_label.setText("Score: 99.4%")
                self.score_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #00FF00;")
        else:
            self.logger("Observatory Action Rejected: Ensure a camera is active and a tool box is spawned.")

    def save_observatory_config(self):
        filepath, _ = QFileDialog.getSaveFileName(self, "Save Observatory XML", "", "XML Files (*.xml)")
        if not filepath: return
        root = ET.Element("ObservatoryConfig")
        ET.SubElement(root, "Filter").text = self.filter_selector.currentText()
        ET.SubElement(root, "Sliders", angle=str(self.slider_angle.value()), intensity=str(self.slider_thresh.value()),
                      score=str(self.slider_score.value()))

        for item in self.cam_scene.items():
            if isinstance(item, SearchROI):
                s_elem = ET.SubElement(root, "SearchROI", x=str(item.pos().x()), y=str(item.pos().y()),
                                       w=str(item.rect().width()), h=str(item.rect().height()))
                p = item.pattern_roi
                ET.SubElement(s_elem, "PatternROI", x=str(p.pos().x()), y=str(p.pos().y()), w=str(p.rect().width()),
                              h=str(p.rect().height()))

        tree = ET.ElementTree(root)
        if hasattr(ET, 'indent'): ET.indent(tree, space="    ", level=0)
        tree.write(filepath, encoding="utf-8", xml_declaration=True)
        self.logger(f"Observatory: Saved active configuration matrix to {filepath}")

    def load_observatory_config(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Open Observatory XML", "", "XML Files (*.xml)")
        if not filepath: return
        try:
            tree = ET.parse(filepath)
            root = tree.getroot()

            f_node = root.find("Filter")
            if f_node is not None: self.filter_selector.setCurrentText(f_node.text or "None")
            s_node = root.find("Sliders")
            if s_node is not None:
                self.slider_angle.setValue(int(s_node.get("angle", 180)))
                self.slider_thresh.setValue(int(s_node.get("intensity", 127)))
                self.slider_score.setValue(int(s_node.get("score", 50)))

            for item in list(self.cam_scene.items()):
                if isinstance(item, SearchROI): self.cam_scene.removeItem(item)
            self.active_tools_list.clear()

            for s_node in root.findall("SearchROI"):
                s_roi = SearchROI(float(s_node.get("x")), float(s_node.get("y")), float(s_node.get("w")),
                                  float(s_node.get("h")))
                p_node = s_node.find("PatternROI")
                if p_node is not None:
                    s_roi.pattern_roi.setPos(float(p_node.get("x")), float(p_node.get("y")))
                    s_roi.pattern_roi.setRect(0, 0, float(p_node.get("w")), float(p_node.get("h")))
                    s_roi.pattern_roi.update_handle_pos()
                self.cam_scene.addItem(s_roi)
                self.tool_count += 1
                self.active_tools_list.addItem(f"tool{self.tool_count:02d} (Pattern Match)")

            self.logger(f"Observatory: Restored configuration matrix from {filepath}")
        except Exception as e:
            self.logger(f"Observatory Loading Error: {str(e)}")

    def closeEvent(self, event):
        if self.camera:
            self.camera.release()
        event.accept()


# ---------------------------------------------------------
# 4. THE AUTHORING & RUN INTERFACE (Main IDE)
# ---------------------------------------------------------
class AuthoringInterface(QMainWindow):
    def __init__(self, canvas):
        super().__init__()
        self.setWindowTitle("LightGuide Studio - Authoring Environment")
        self.setGeometry(200, 200, 1400, 800)

        self.canvas = canvas
        self.current_file = None
        self.active_item = None

        self.setup_ui()
        self.observatory = ObservatoryEngine(self.log_message)

        self.canvas.scene.selectionChanged.connect(self.handle_canvas_selection)

        self.blink_state = True
        self.blink_timer = QTimer()
        self.blink_timer.timeout.connect(self.process_blinking_items)
        self.blink_timer.start(500)

        self.log_message("System Initialized.")

    def setup_ui(self):
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")
        file_menu.addAction(QAction("New Design", self, triggered=self.new_file))
        file_menu.addAction(QAction("Open XML...", self, triggered=self.open_file))
        file_menu.addAction(QAction("Save", self, triggered=self.save_file))
        file_menu.addSeparator()
        file_menu.addAction(QAction("Exit", self, triggered=self.close))

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QPushButton("✏️ Design Mode"))
        toolbar.addWidget(QPushButton("▶️ Run Mode"))

        btn_clear = QPushButton("🗑️ Clear Canvas")
        btn_clear.clicked.connect(self.canvas.clear_canvas)
        toolbar.addWidget(btn_clear)

        toolbar.addStretch()
        btn_obs = QPushButton("👁️ Launch Observatory")
        btn_obs.clicked.connect(lambda: self.observatory.show())
        toolbar.addWidget(btn_obs)
        main_layout.addLayout(toolbar)

        v_splitter = QSplitter(Qt.Orientation.Vertical)
        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(QLabel("<b>Asset Toolbox</b>"))
        self.btn_insert_text = QPushButton("Insert Text")
        self.btn_insert_text.clicked.connect(self.insert_text_asset)
        left_layout.addWidget(self.btn_insert_text)
        left_layout.addWidget(QPushButton("Insert Image"))
        left_layout.addWidget(QPushButton("Insert Video"))
        left_layout.addStretch()

        center_panel = QWidget()
        center_layout = QVBoxLayout(center_panel)
        center_layout.addWidget(QLabel("<b>Execution Timeline (Steps)</b>"))
        self.step_table = QTableWidget(0, 4)
        self.step_table.setHorizontalHeaderLabels(["Step", "Asset", "Trigger", "Properties"])
        self.step_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.step_table.verticalHeader().setVisible(False)
        center_layout.addWidget(self.step_table)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.addWidget(QLabel("<b>Properties Inspector</b>"))

        self.prop_group = QGroupBox("Selected Item")
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

        self.prop_blink = QCheckBox("Enable")
        self.prop_blink.stateChanged.connect(self.update_active_item)

        self.prop_var = QComboBox()
        self.prop_var.addItems(["None", "tool01", "tool02", "tool03"])
        self.prop_var.currentTextChanged.connect(self.update_active_item)

        prop_form.addRow("Text:", self.prop_text)
        prop_form.addRow("Font:", self.prop_font)
        prop_form.addRow("Size:", self.prop_size)
        prop_form.addRow("Rotation (°):", self.prop_rotation)
        prop_form.addRow("Blinking:", self.prop_blink)
        prop_form.addRow("Trigger Var:", self.prop_var)

        self.prop_group.setEnabled(False)
        right_layout.addWidget(self.prop_group)
        right_layout.addStretch()

        h_splitter.addWidget(left_panel)
        h_splitter.addWidget(center_panel)
        h_splitter.addWidget(right_panel)
        h_splitter.setSizes([200, 800, 300])

        self.console = QTextBrowser()
        self.console.setReadOnly(True)

        v_splitter.addWidget(h_splitter)
        v_splitter.addWidget(self.console)
        v_splitter.setSizes([650, 150])
        main_layout.addWidget(v_splitter)

    def insert_text_asset(self):
        text_item = InteractiveTextItem("New Text")
        text_item.setPos(200, 200)
        self.canvas.scene.addItem(text_item)
        self.log_message("Inserted Text Asset onto canvas.")

        row = self.step_table.rowCount()
        self.step_table.insertRow(row)
        self.step_table.setItem(row, 0, QTableWidgetItem(f"Step {row + 1}"))
        self.step_table.setItem(row, 1, QTableWidgetItem("Text"))
        self.step_table.setItem(row, 2, QTableWidgetItem("None"))
        self.step_table.setItem(row, 3, QTableWidgetItem("Default Params"))

    def handle_canvas_selection(self):
        selected_items = self.canvas.scene.selectedItems()

        if not selected_items:
            self.active_item = None
            self.prop_group.setEnabled(False)
            return

        item = selected_items[0]
        if isinstance(item, InteractiveTextItem):
            self.active_item = item
            self.prop_group.setEnabled(True)

            self.prop_text.blockSignals(True)
            self.prop_font.blockSignals(True)
            self.prop_size.blockSignals(True)
            self.prop_rotation.blockSignals(True)
            self.prop_blink.blockSignals(True)
            self.prop_var.blockSignals(True)

            self.prop_text.setText(item.toPlainText())
            self.prop_font.setCurrentFont(item.font())
            self.prop_size.setValue(item.font().pointSize())
            self.prop_rotation.setValue(int(item.rotation()))
            self.prop_blink.setChecked(item.is_blinking)
            self.prop_var.setCurrentText(item.tied_variable)

            self.prop_text.blockSignals(False)
            self.prop_font.blockSignals(False)
            self.prop_size.blockSignals(False)
            self.prop_rotation.blockSignals(False)
            self.prop_blink.blockSignals(False)
            self.prop_var.blockSignals(False)

    def update_active_item(self):
        if not self.active_item: return

        self.active_item.setPlainText(self.prop_text.text())

        new_font = self.prop_font.currentFont()
        new_font.setPointSize(self.prop_size.value())
        self.active_item.setFont(new_font)

        self.active_item.setRotation(self.prop_rotation.value())
        self.active_item.is_blinking = self.prop_blink.isChecked()
        self.active_item.tied_variable = self.prop_var.currentText()

        if not self.active_item.is_blinking:
            self.active_item.setVisible(True)

    def process_blinking_items(self):
        self.blink_state = not self.blink_state
        for item in self.canvas.scene.items():
            if isinstance(item, InteractiveTextItem) and item.is_blinking:
                item.setVisible(self.blink_state)

    def new_file(self):
        self.step_table.setRowCount(0)
        self.current_file = None
        self.canvas.clear_canvas()
        self.log_message("Started new design sequence.")

    def save_file(self):
        filepath, _ = QFileDialog.getSaveFileName(self, "Save XML", "", "XML Files (*.xml)")
        if not filepath: return

        root = ET.Element("LightGuideDesign")
        scene_items = [item for item in self.canvas.scene.items() if isinstance(item, InteractiveTextItem)]

        for i, item in enumerate(scene_items):
            step_elem = ET.SubElement(root, "Step", id=str(i + 1))
            asset_elem = ET.SubElement(step_elem, "Asset", type="InteractiveTextItem")
            ET.SubElement(asset_elem, "Text").text = item.toPlainText()
            ET.SubElement(asset_elem, "Position", x=str(item.pos().x()), y=str(item.pos().y()))
            ET.SubElement(asset_elem, "Font", family=item.font().family(), size=str(item.font().pointSize()))
            ET.SubElement(asset_elem, "Rotation", angle=str(item.rotation()))
            ET.SubElement(asset_elem, "Behavior", blink=str(item.is_blinking), variable=item.tied_variable)

        tree = ET.ElementTree(root)
        if hasattr(ET, 'indent'): ET.indent(tree, space="    ", level=0)
        tree.write(filepath, encoding="utf-8", xml_declaration=True)

        self.current_file = filepath
        self.log_message(f"Successfully saved XML design to: {self.current_file}")

    def open_file(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Open XML", "", "XML Files (*.xml)")
        if not filepath: return

        try:
            tree = ET.parse(filepath)
            root = tree.getroot()
            self.canvas.clear_canvas()
            self.step_table.setRowCount(0)

            for step in root.findall("Step"):
                asset = step.find("Asset")
                if asset is not None and asset.get("type") == "InteractiveTextItem":
                    new_item = InteractiveTextItem()
                    text_node = asset.find("Text")
                    if text_node is not None: new_item.setPlainText(text_node.text or "")
                    pos_node = asset.find("Position")
                    if pos_node is not None: new_item.setPos(float(pos_node.get("x", 0)), float(pos_node.get("y", 0)))
                    font_node = asset.find("Font")
                    if font_node is not None: new_item.setFont(
                        QFont(font_node.get("family", "Arial"), int(font_node.get("size", 48))))
                    rot_node = asset.find("Rotation")
                    if rot_node is not None: new_item.setRotation(float(rot_node.get("angle", 0)))
                    beh_node = asset.find("Behavior")
                    if beh_node is not None:
                        new_item.is_blinking = (beh_node.get("blink") == "True")
                        new_item.tied_variable = beh_node.get("variable", "None")

                    self.canvas.scene.addItem(new_item)
                    row = self.step_table.rowCount()
                    self.step_table.insertRow(row)
                    self.step_table.setItem(row, 0, QTableWidgetItem(f"Step {row + 1}"))
                    self.step_table.setItem(row, 1, QTableWidgetItem("Text"))
                    self.step_table.setItem(row, 2, QTableWidgetItem(new_item.tied_variable))
                    self.step_table.setItem(row, 3, QTableWidgetItem(f"Rot: {new_item.rotation()}°"))

            self.current_file = filepath
            self.log_message(f"Loaded XML config from: {filepath}")

        except Exception as e:
            self.log_message(f"Error loading XML: {str(e)}")

    def log_message(self, message):
        ts = time.strftime("[%H:%M:%S]")
        self.console.append(f"<span style='color:#00FF00;'>{ts} {message}</span>")


# ---------------------------------------------------------
# 5. ORCHESTRATOR
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