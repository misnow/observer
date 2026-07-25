import sys
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QGraphicsView, QGraphicsScene,
                             QLabel, QSplitter, QComboBox, QTreeWidget, QTreeWidgetItem,
                             QTextBrowser, QHeaderView, QFileDialog, QFormLayout,
                             QLineEdit, QSpinBox, QGroupBox)
from PyQt6.QtCore import pyqtSignal, QTimer, Qt
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import QGraphicsItem

# The modular import linking to observatory.py
from observatory import (ObservatoryEngine, ProjectorCanvas, InteractiveTextItem,
                         InteractiveMediaItem, FlowControlData, ConfirmationData, DARK_THEME)


# --- STUDIO CANVAS VIEW ---
class StudioCanvasView(QGraphicsView):
    def __init__(self, scene, parent_interface):
        super().__init__(scene);
        self.parent_interface = parent_interface;
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                ext = url.toLocalFile().lower().split('.')[-1]
                t = "Video" if ext in ['mp4', 'avi', 'mkv'] else "Audio" if ext in ['mp3', 'wav'] else "Image"
                self.parent_interface.insert_asset(InteractiveMediaItem(url.toLocalFile(), t), f"Media [{t}]")
            event.acceptProposedAction()


# --- AUTHORING INTERFACE ---
class AuthoringInterface(QMainWindow):
    def __init__(self, canvas):
        super().__init__()
        self.setWindowTitle("LightGuide Studio - Authoring Environment")
        self.setGeometry(200, 200, 1500, 850)
        self.canvas, self.studio_scene = canvas, canvas.scene
        self.active_item = None
        self.is_running = False
        self.execution_sequence = []
        self.pc = 0
        self.wait_state = "IDLE"
        self.manual_pass_flag = False

        self.setup_ui()
        self.observatory = ObservatoryEngine(self.log_message)
        self.observatory.show()

        self.step_tree.itemSelectionChanged.connect(self.handle_tree_selection)
        self.master_clock = QTimer()
        self.master_clock.timeout.connect(self.process_execution_engine)
        self.master_clock.start(100)
        self.enter_design_mode()

    def setup_ui(self):
        main_layout = QVBoxLayout()
        central = QWidget();
        central.setLayout(main_layout);
        self.setCentralWidget(central)

        # Toolbar
        toolbar = QHBoxLayout()
        self.btn_design_mode = QPushButton("✏️ DESIGN MODE");
        self.btn_design_mode.clicked.connect(self.enter_design_mode)
        self.btn_run_mode = QPushButton("▶️ RUN MODE");
        self.btn_run_mode.clicked.connect(self.enter_run_mode)
        self.btn_manual_confirm = QPushButton("✅ CONFIRM / PASS STEP")
        self.btn_manual_confirm.setStyleSheet("background-color: #f39c12; font-weight: bold; color: black;")
        self.btn_manual_confirm.clicked.connect(self.trigger_manual_pass);
        self.btn_manual_confirm.setEnabled(False)
        btn_obs = QPushButton("👁️ Bring Vision Engine Forward");
        btn_obs.clicked.connect(lambda: self.observatory.raise_())

        toolbar.addWidget(self.btn_design_mode);
        toolbar.addWidget(self.btn_run_mode);
        toolbar.addWidget(self.btn_manual_confirm)
        toolbar.addStretch();
        toolbar.addWidget(btn_obs);
        main_layout.addLayout(toolbar)

        # Main Layout
        h_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.step_tree = QTreeWidget()
        self.step_tree.setHeaderLabels(["#", "Sequence Pipeline", "Action / Trigger", "Properties"])
        h_splitter.addWidget(self.step_tree)

        self.console = QTextBrowser();
        self.console.setReadOnly(True)
        main_layout.addWidget(h_splitter);
        main_layout.addWidget(self.console)

        self.studio_override_view = StudioCanvasView(self.studio_scene, self)
        self.canvas.setCentralWidget(self.studio_override_view)
        self.new_file()

    def process_execution_engine(self):
        if not self.is_running or self.pc >= len(self.execution_sequence): return
        step = self.execution_sequence[self.pc]
        if self.wait_state == "IDLE":
            for item in self.studio_scene.items(): item.setVisible(False)
            for asset in step["assets"]: asset.setVisible(True)
            self.wait_state = "WAITING_CONFIRMATION";
            self.manual_pass_flag = False
        elif self.wait_state == "WAITING_CONFIRMATION":
            ready = False
            if not step["conf_node"]:
                ready = True
            elif step["conf_node"].wait_type == "Manual Confirmation" and self.manual_pass_flag:
                ready = True
            elif step["conf_node"].wait_type == "Vision Variable" and self.observatory.tool_states.get(
                step["conf_node"].vision_var, False):
                ready = True

            if ready:
                self.pc += 1;
                self.wait_state = "IDLE";
                self.manual_pass_flag = False

    def enter_design_mode(self):
        self.is_running = False;
        self.btn_manual_confirm.setEnabled(False)
        for item in self.studio_scene.items(): item.setVisible(True)

    def enter_run_mode(self):
        self.is_running = True;
        self.btn_manual_confirm.setEnabled(True)
        for item in self.studio_scene.items(): item.setVisible(False)
        self.execution_sequence = self.build_execution_sequence()
        self.pc, self.wait_state = 0, "IDLE"

    def trigger_manual_pass(self):
        self.manual_pass_flag = True

    def build_execution_sequence(self):
        seq = []
        for i in range(self.step_tree.topLevelItemCount()):
            top = self.step_tree.topLevelItem(i);
            step = {"assets": [], "conf_node": None}
            for j in range(top.childCount()):
                asset = top.child(j).data(1, Qt.ItemDataRole.UserRole)
                if isinstance(asset, ConfirmationData):
                    step["conf_node"] = asset
                elif asset:
                    step["assets"].append(asset)
            seq.append(step)
        return seq

    def add_new_step_node(self):
        idx = self.step_tree.topLevelItemCount() + 1
        item = QTreeWidgetItem(self.step_tree, ["", f"Step Block [{idx}]", "", ""])
        self.step_tree.setCurrentItem(item)

    def renumber_sequence(self):
        for i in range(self.step_tree.topLevelItemCount()):
            top = self.step_tree.topLevelItem(i);
            top.setText(0, str(i + 1))
            for j in range(top.childCount()): top.child(j).setText(0, f"{i + 1}.{j + 1}")

    def insert_asset(self, item, name):
        parent = self.step_tree.currentItem() or self.step_tree.topLevelItem(0)
        ti = QTreeWidgetItem(parent, ["", name, "Active", ""]);
        ti.setData(1, Qt.ItemDataRole.UserRole, item)
        self.renumber_sequence()

    def insert_confirmation_node(self):
        parent = self.step_tree.currentItem() or self.step_tree.topLevelItem(0)
        ti = QTreeWidgetItem(parent, ["", "⏳ Confirmation", "Manual", ""]);
        ti.setData(1, Qt.ItemDataRole.UserRole, ConfirmationData())
        self.renumber_sequence()

    def log_message(self, msg):
        self.console.append(msg)

    def handle_tree_selection(self):
        pass

    def handle_canvas_selection(self):
        pass

    def new_file(self):
        self.add_new_step_node()


def main():
    app = QApplication(sys.argv);
    app.setStyleSheet(DARK_THEME)
    canvas = ProjectorCanvas();
    authoring = AuthoringInterface(canvas)
    canvas.show();
    authoring.show();
    sys.exit(app.exec())


if __name__ == "__main__": main()