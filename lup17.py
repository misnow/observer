import sys
import os
import cv2
import numpy as np
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *

# Silence OpenCV
os.environ["OPENCV_LOG_LEVEL"] = "FATAL"

# MediaPipe safety
try:
    import mediapipe as mp

    mp_pose = mp.solutions.pose
    mp_drawing = mp.solutions.drawing_utils
    MP_AVAILABLE = True
except:
    MP_AVAILABLE = False


# --- DATA STRUCTURES ---
class FlowControlData:
    def __init__(self, action="End Program Execution", target_step=1):
        self.action, self.target_step = action, target_step


class ConfirmationData:
    def __init__(self, wait_type="Manual Confirmation", vision_var="None"):
        self.wait_type, self.vision_var = wait_type, vision_var


# --- VISION TOOLS ---
class SearchROI(QGraphicsRectItem):
    def __init__(self, x, y, tool_name, tool_type):
        super().__init__(0, 0, 250, 250)
        self.tool_name, self.tool_type = tool_name, tool_type
        self.threshold, self.sensitivity, self.current_score = 127, 50, 0
        self.trained_template, self.reference_frame = None, None
        self.setPos(x, y);
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.label = QGraphicsTextItem(f"{tool_name}\n[{tool_type}]", self)


class PerspectivePlaneROI(QGraphicsPolygonItem):
    def __init__(self, x, y, tool_name):
        super().__init__()
        self.tool_name, self.tool_type = tool_name, "Reference Plane"
        self.homography_matrix = None
        self.points = [QPointF(0, 0), QPointF(200, 0), QPointF(200, 200), QPointF(0, 200)]
        self.setPolygon(QPolygonF(self.points))
        self.setPos(x, y);
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)


# --- OBSERVATORY ENGINE ---
class ObservatoryEngine(QMainWindow):
    def __init__(self, logger):
        super().__init__()
        self.logger, self.tool_states = logger, {"None": True}
        self.camera = None;
        self.timer = QTimer();
        self.timer.timeout.connect(self.update_frame)
        self.setup_ui()

    def setup_ui(self):
        self.setWindowTitle("Observatory Engine")
        self.cam_scene = QGraphicsScene();
        self.view = QGraphicsView(self.cam_scene)
        self.video_frame_item = self.cam_scene.addPixmap(QPixmap())
        self.setCentralWidget(self.view)
        self.btn = QPushButton("Toggle Camera");
        self.btn.clicked.connect(self.toggle_camera)
        self.btn.setParent(self.view)

    def toggle_camera(self):
        if not self.camera or not self.camera.isOpened():
            self.camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            self.timer.start(30)
        else:
            self.timer.stop(); self.camera.release()

    def update_frame(self):
        ret, frame = self.camera.read()
        if not ret: return
        # Vision Logic
        for item in self.cam_scene.items():
            if hasattr(item, 'tool_type'):
                x, y = int(item.pos().x()), int(item.pos().y())
                w, h = 200, 200  # Simplified bounds
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        self.video_frame_item.setPixmap(
            QPixmap.fromImage(QImage(rgb.data, rgb.shape[1], rgb.shape[0], QImage.Format.Format_RGB888)))


# --- AUTHORING STUDIO ---
class AuthoringInterface(QMainWindow):
    def __init__(self, canvas):
        super().__init__()
        self.observatory = ObservatoryEngine(lambda x: print(x))
        self.setup_ui()
        self.new_file()

    def setup_ui(self):
        self.setWindowTitle("Studio Authoring")
        self.tree = QTreeWidget();
        self.setCentralWidget(self.tree)
        self.tree.setHeaderLabels(["#", "Action", "Status"])
        btn = QPushButton("Add Step");
        btn.clicked.connect(self.add_step)
        self.addToolBar("Main").addWidget(btn)

    def add_step(self):
        idx = self.tree.topLevelItemCount() + 1
        item = QTreeWidgetItem(self.tree, [str(idx), f"Step {idx}", "Ready"])
        self.tree.setCurrentItem(item)

    def new_file(self): self.add_step()


# --- MAIN ---
def main():
    app = QApplication(sys.argv)
    # ProjectorCanvas can be integrated here if needed
    studio = AuthoringInterface(QMainWindow())
    studio.show()
    sys.exit(app.exec())


if __name__ == "__main__": main()