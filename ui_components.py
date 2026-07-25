import json
import urllib.request
import urllib.error
from PyQt6.QtWidgets import QWidget, QGraphicsView, QFormLayout, QComboBox, QLineEdit
from PyQt6.QtCore import pyqtSignal, Qt, QThread, QRect
from PyQt6.QtGui import QColor, QPen, QFont, QPainter

# Professional Dark Theme Stylesheet
DARK_THEME = """
QWidget { background-color: #2b2b2b; color: #a9b7c6; font-family: 'Segoe UI', Arial; }
QPushButton { background-color: #4C5052; border: 1px solid #5C5C42; padding: 5px 15px; border-radius: 3px; color: #ffffff; }
QPushButton:hover { background-color: #5C6062; }
QPushButton:pressed { background-color: #3C3F41; }
QListWidget { background-color: #313335; border: 1px solid #1e1e1e; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QTextEdit { background-color: #3C3F41; border: 1px solid #1e1e1e; padding: 3px; color: #ffffff; }
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right; width: 16px;
    border-left: 1px solid #1e1e1e; border-bottom: 1px solid #1e1e1e; background-color: #4C5052;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right; width: 16px;
    border-left: 1px solid #1e1e1e; border-top: 1px solid #1e1e1e; background-color: #4C5052;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover { background-color: #5C6062; }
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed { background-color: #3C3F41; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent; border-bottom: 5px solid #ffffff;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 5px solid #ffffff;
}
QMenuBar { background-color: #3C3F41; border-bottom: 1px solid #1e1e1e; }
QMenuBar::item:selected { background-color: #2f65ca; }
QMenu { background-color: #313335; border: 1px solid #555; }
QMenu::item:selected { background-color: #2f65ca; }
QGroupBox { border: 1px solid #555555; margin-top: 10px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
QSlider::groove:horizontal { border: 1px solid #555; height: 8px; background: #3C3F41; margin: 2px 0; border-radius: 4px; }
QSlider::handle:horizontal { background: #4C5052; border: 1px solid #5C5C42; width: 14px; margin: -4px 0; border-radius: 7px; }
QTreeWidget { background-color: #1e1e1e; border: 1px solid #555; color: #a9b7c6; }
QTreeWidget::item { padding: 4px; border-bottom: 1px solid #2b2b2b; }
QTreeWidget::item:selected { background-color: #2f65ca; color: white; }
QHeaderView::section { background-color: #3C3F41; padding: 4px; border: 1px solid #1e1e1e; font-weight: bold; }
QTextBrowser { background-color: #1e1e1e; color: #a9b7c6; border: 1px solid #555555; font-family: 'Consolas', monospace; }
"""


class LLMWorker(QThread):
    """Runs one LLM request in the background against whichever provider is
    configured. `provider_config` is the dict produced by
    LLMProviderSettingsWidget.get_config() - either:
      {"provider": "Gemini", "api_key": "..."}
    or:
      {"provider": "OpenAI-Compatible", "base_url": "...", "model": "...", "api_key": "..."}
    The OpenAI-Compatible path covers Ollama, Open WebUI, LM Studio, vLLM,
    and most private-cloud LLM deployments, since they all speak the same
    chat-completions schema."""
    finished = pyqtSignal(str)

    def __init__(self, provider_config, prompt, base64_image=None, parent=None):
        super().__init__(parent)
        self.config = provider_config or {}
        self.prompt = prompt
        self.base64_image = base64_image
        self.response = ""

    def run(self):
        try:
            if self.config.get("provider") == "OpenAI-Compatible":
                self._run_openai_compatible()
            else:
                self._run_gemini()
        except urllib.error.HTTPError as e:
            error_msg = e.read().decode('utf-8')
            self.finished.emit(f"API Error ({e.code}): {error_msg}")
        except Exception as e:
            self.finished.emit(f"Connection Error: {str(e)}")

    def _post_json(self, url, payload, headers):
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.loads(response.read().decode('utf-8'))

    def _run_gemini(self):
        api_key = self.config.get("api_key", "")
        if not api_key:
            self.finished.emit("Error: Gemini API Key not provided.")
            return

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"

        parts = [{"text": self.prompt}]
        if self.base64_image:
            parts.append({"inline_data": {"mime_type": "image/png", "data": self.base64_image}})

        result = self._post_json(url, {"contents": [{"parts": parts}]}, {'Content-Type': 'application/json'})
        self.response = result['candidates'][0]['content']['parts'][0]['text']
        self.finished.emit(self.response)

    def _run_openai_compatible(self):
        base_url = self.config.get("base_url", "").strip().rstrip("/")
        model = self.config.get("model", "").strip()
        api_key = self.config.get("api_key", "").strip()
        if not base_url or not model:
            self.finished.emit("Error: OpenAI-Compatible Base URL and Model must both be set.")
            return

        url = f"{base_url}/chat/completions"
        content = [{"type": "text", "text": self.prompt}]
        if self.base64_image:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{self.base64_image}"}})

        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['Authorization'] = f"Bearer {api_key}"

        payload = {"model": model, "messages": [{"role": "user", "content": content}], "stream": False}
        result = self._post_json(url, payload, headers)
        self.response = result['choices'][0]['message']['content']
        self.finished.emit(self.response)


class LLMProviderSettingsWidget(QWidget):
    """Provider picker + settings, reused by both Studio and Observatory.
    Persists to the QSettings instance passed in (both apps already share
    the same QSettings("LightGuide", "Observatory") namespace, so entering
    settings once in either window covers both). Call get_config() to build
    the dict LLMWorker expects."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings

        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)

        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["Gemini", "OpenAI-Compatible (Ollama / Open WebUI / Custom)"])
        self.form.addRow("LLM Provider:", self.provider_combo)

        self.gemini_key_input = QLineEdit()
        self.gemini_key_input.setPlaceholderText("Gemini API Key...")
        self.gemini_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.form.addRow("Gemini API Key:", self.gemini_key_input)

        self.base_url_input = QLineEdit()
        self.base_url_input.setPlaceholderText("http://localhost:11434/v1")
        self.form.addRow("Base URL:", self.base_url_input)

        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("e.g. llava, moondream, qwen2-vl")
        self.form.addRow("Model:", self.model_input)

        self.openai_key_input = QLineEdit()
        self.openai_key_input.setPlaceholderText("API Key (leave blank for local Ollama)...")
        self.openai_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.form.addRow("API Key:", self.openai_key_input)

        # Load persisted values before wiring up save-on-change, so loading
        # doesn't immediately re-save what was just read.
        self.provider_combo.setCurrentText(str(self.settings.value("llm_provider", "Gemini")))
        # "api_key" was the old, pre-multi-provider setting name - fall back
        # to it so an already-saved Gemini key carries over automatically.
        self.gemini_key_input.setText(
            str(self.settings.value("gemini_api_key", self.settings.value("api_key", ""))))
        self.base_url_input.setText(str(self.settings.value("openai_base_url", "http://localhost:11434/v1")))
        self.model_input.setText(str(self.settings.value("openai_model", "")))
        self.openai_key_input.setText(str(self.settings.value("openai_api_key", "")))

        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)
        self.gemini_key_input.textChanged.connect(lambda t: self.settings.setValue("gemini_api_key", t.strip()))
        self.base_url_input.textChanged.connect(lambda t: self.settings.setValue("openai_base_url", t.strip()))
        self.model_input.textChanged.connect(lambda t: self.settings.setValue("openai_model", t.strip()))
        self.openai_key_input.textChanged.connect(lambda t: self.settings.setValue("openai_api_key", t.strip()))

        self._on_provider_changed(self.provider_combo.currentText())

    def _on_provider_changed(self, provider):
        self.settings.setValue("llm_provider", provider)
        is_gemini = provider == "Gemini"
        for widget, visible in [(self.gemini_key_input, is_gemini),
                                 (self.base_url_input, not is_gemini),
                                 (self.model_input, not is_gemini),
                                 (self.openai_key_input, not is_gemini)]:
            widget.setVisible(visible)
            label = self.form.labelForField(widget)
            if label: label.setVisible(visible)

    def get_config(self):
        if self.provider_combo.currentText() == "Gemini":
            return {"provider": "Gemini", "api_key": self.gemini_key_input.text().strip()}
        return {
            "provider": "OpenAI-Compatible",
            "base_url": self.base_url_input.text().strip(),
            "model": self.model_input.text().strip(),
            "api_key": self.openai_key_input.text().strip(),
        }


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
                         QColor(0, 200, 0, 180) if self.score >= self.threshold else QColor(220, 0, 0, 180))
        t_x = int((self.threshold / 100.0) * rect.width())
        painter.setPen(QPen(QColor("white"), 3))
        painter.drawLine(t_x, 0, t_x, rect.height())
        painter.setPen(QColor("white"))
        painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                         f"Signal Score: {self.score}%  |  Trigger Line: {self.threshold}%")

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