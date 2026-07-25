import sys
import os
import time
import xml.etree.ElementTree as ET

# Silence OpenCV C++ probing errors on Windows
os.environ["OPENCV_LOG_LEVEL"] = "FATAL"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"

import cv2
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QGraphicsView, QGraphicsScene,
                             QLabel, QSplitter, QComboBox, QToolBar, QStatusBar,
                             QMenuBar, QTableWidget, QTableWidgetItem, QTextBrowser,
                             QHeaderView, QFileDialog, QGraphicsTextItem, QFormLayout,
                             QLineEdit, QSpinBox, QCheckBox, QFontComboBox, QGroupBox,
                             QSizePolicy)
from PyQt6.QtCore import pyqtSignal, QTimer, Qt
from PyQt6.QtGui import QColor, QBrush, QImage, QPixmap, QAction, QFont

# Professional Dark Theme Stylesheet
DARK_THEME = """
QWidget { background-color: #2b2b2b; color: #a9b7c6; font-family: 'Segoe UI', Arial; }
QPushButton { background-color: #4C5052; border: 1px solid #5C5C42; padding: 5px 15px; border-radius: 3px; color: #ffffff; }
QPushButton:hover { background-color: #5C6062; }
QPushButton:pressed { background-color: #3C3F41; }
QPushButton:disabled { background-color: #1e1e1e; color: #555555; border: 1px solid #333333; }
QTableWidget { background-color: #313335; gridline-color: #555555; border: 1px solid #1e1e1e; }
QHeaderView::section { background-color: #3C3F41; padding: 4px; border: 1px solid #1e1e1e; }
QTextBrowser { background-color: #1e1e1e; color: #a9b7c6; border: 1px solid #555555; font-family: 'Consolas', monospace; }
QComboBox, QSpinBox, QLineEdit { background-color: #3C3F41; border: 1px solid #1e1e1e; padding: 3px; color: #ffffff; }
QMenuBar { background-color: #3C3F41; border-bottom: 1px solid #1e1e1e; }
QMenuBar::item:selected { background-color: #5C6062; }
QGroupBox { border: 1px solid #555555; margin-top: 10px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
"""


# ---------------------------------------------------------
# 1. CUSTOM CANVAS ASSETS (Interactive Objects)
# ---------------------------------------------------------
class InteractiveTextItem(QGraphicsTextItem):
    """A custom text item that supports dragging, middle-click rotation, and custom properties."""

    def __init__(self, text="New Text"):
        super().__init__(text)

        # Enable dragging and selecting
        self.setFlags(QGraphicsTextItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsTextItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsTextItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        self.setDefaultTextColor(QColor("white"))
        self.setFont(QFont("Arial", 48))

        # Authoring logic properties
        self.is_blinking = False
        self.tied_variable = "None"

        # Internal state for rotation
        self._is_rotating = False

        # Center the origin point for clean rotation
        self.update_transform_origin()

    def update_transform_origin(self):
        """Ensures the item rotates around its center, not its top-left corner."""
        rect = self.boundingRect()
        self.setTransformOriginPoint(rect.width() / 2, rect.height() / 2)

    def setPlainText(self, text):
        """Override to ensure origin updates when text length changes."""
        super().setPlainText(text)
        self.update_transform_origin()

    # --- Mouse Events for Middle-Click Rotation ---
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_rotating = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._is_rotating:
            # Simple rotation drag: moving mouse up/down rotates the item
            delta_y = event.pos().y() - event.lastPos().y()
            new_angle = (self.rotation() + delta_y) % 360
            self.setRotation(new_angle)

            # Notify the scene that geometry changed so the UI can update
            self.scene().update()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_rotating = False
            self.setCursor(Qt.CursorShape.ArrowCursor)

            # Ensure the property panel updates to the new exact angle on release
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

        # Make the scene large enough to act as a projector area
        self.scene.setSceneRect(0, 0, 1920, 1080)

        self.view = QGraphicsView(self.scene)
        self.view.setStyleSheet("border: none; background-color: black;")
        self.setCentralWidget(self.view)

    def clear_canvas(self):
        self.scene.clear()


# ---------------------------------------------------------
# 3. THE OBSERVATORY ENGINE (Vision)
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

        # Prevent the video label from dictating layout size to stop infinite UI zooming
        self.video_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.layout.addWidget(self.video_label, 1)

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

                # Request a standard 720p resolution
                self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

                # Lock down hardware autofocus and autozoom for stability
                self.camera.set(cv2.CAP_PROP_AUTOFOCUS, 0)
                self.camera.set(cv2.CAP_PROP_ZOOM, 1)

                if self.camera.isOpened():
                    self.timer.start(30)
                    self.btn_toggle_cam.setText("Stop Camera")
                    self.logger(f"Camera {cam_idx} started.")

    def update_frame(self):
        if self.camera and self.camera.isOpened():
            ret, frame = self.camera.read()
            if ret:
                rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb_image.shape
                qt_image = QImage(rgb_image.data, w, h, ch * w, QImage.Format.Format_RGB888)

                # Ensure we scale strictly to the current static bounds of the label
                label_size = self.video_label.size()

                if label_size.width() > 0 and label_size.height() > 0:
                    pixmap = QPixmap.fromImage(qt_image).scaled(
                        label_size,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation
                    )
                    self.video_label.setPixmap(pixmap)

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

        # Connect Canvas Selection to Properties Panel
        self.canvas.scene.selectionChanged.connect(self.handle_canvas_selection)

        # Blink Engine Timer
        self.blink_state = True
        self.blink_timer = QTimer()
        self.blink_timer.timeout.connect(self.process_blinking_items)
        self.blink_timer.start(500)

        self.log_message("System Initialized.")

    def setup_ui(self):
        # Menu Bar
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

        # Tool Bar
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

        # Splitters
        v_splitter = QSplitter(Qt.Orientation.Vertical)
        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        # --- LEFT PANEL: Toolbox ---
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(QLabel("<b>Asset Toolbox</b>"))

        self.btn_insert_text = QPushButton("Insert Text")
        self.btn_insert_text.clicked.connect(self.insert_text_asset)
        left_layout.addWidget(self.btn_insert_text)

        left_layout.addWidget(QPushButton("Insert Image"))
        left_layout.addWidget(QPushButton("Insert Video"))
        left_layout.addStretch()

        # --- CENTER PANEL: Execution Timeline ---
        center_panel = QWidget()
        center_layout = QVBoxLayout(center_panel)
        center_layout.addWidget(QLabel("<b>Execution Timeline (Steps)</b>"))
        self.step_table = QTableWidget(0, 4)
        self.step_table.setHorizontalHeaderLabels(["Step", "Asset", "Trigger", "Properties"])
        self.step_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.step_table.verticalHeader().setVisible(False)
        center_layout.addWidget(self.step_table)

        # --- RIGHT PANEL: Properties Inspector ---
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
        self.prop_var.addItems(["None", "tool01_true", "tool01_false", "tool02_true"])
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

        # Build Layout
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

    # --- Canvas & Property Logic ---
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

    # --- File Operations ---
    def new_file(self):
        self.step_table.setRowCount(0)
        self.current_file = None
        self.canvas.clear_canvas()
        self.log_message("Started new design sequence.")

    def save_file(self):
        filepath, _ = QFileDialog.getSaveFileName(self, "Save XML", "", "XML Files (*.xml)")
        if not filepath:
            return

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

        if hasattr(ET, 'indent'):
            ET.indent(tree, space="    ", level=0)

        tree.write(filepath, encoding="utf-8", xml_declaration=True)

        self.current_file = filepath
        self.log_message(f"Successfully saved XML design to: {self.current_file}")

    def open_file(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Open XML", "", "XML Files (*.xml)")
        if not filepath:
            return

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
                    if text_node is not None:
                        new_item.setPlainText(text_node.text or "")

                    pos_node = asset.find("Position")
                    if pos_node is not None:
                        new_item.setPos(float(pos_node.get("x", 0)), float(pos_node.get("y", 0)))

                    font_node = asset.find("Font")
                    if font_node is not None:
                        font = QFont(font_node.get("family", "Arial"), int(font_node.get("size", 48)))
                        new_item.setFont(font)

                    rot_node = asset.find("Rotation")
                    if rot_node is not None:
                        new_item.setRotation(float(rot_node.get("angle", 0)))

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