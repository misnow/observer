import re
import cv2
import numpy as np
import base64
from PyQt6.QtWidgets import QGraphicsRectItem, QGraphicsEllipseItem, QGraphicsPolygonItem, QGraphicsTextItem, \
    QGraphicsItem
from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QColor, QBrush, QPen, QPolygonF, QFont


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
        # Which camera this tool belongs to. Lets one Observatory project
        # hold tools for several cameras at once - only the tools bound to
        # the camera currently being viewed are drawn and evaluated.
        self.camera_index = 0

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
        self.points[index] = pos
        self.update_visual_polygon()

    def update_visual_polygon(self):
        self.setPolygon(QPolygonF(self.points))

    def reposition_handles(self):
        for i, h in enumerate(self.handles): h.setPos(self.points[i])


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
        # Which camera this tool belongs to. Lets one Observatory project
        # hold tools for several cameras at once - only the tools bound to
        # the camera currently being viewed are drawn and evaluated.
        self.camera_index = 0

        # Core Properties
        # "threshold" means different things per tool type, so its default
        # has to be per-type rather than one shared number:
        #   Motion Detection / Delete - a per-pixel frame-difference cutoff
        #     (0-255). 127 demanded that a pixel change by half the full
        #     dynamic range, which ordinary motion under normal lighting
        #     never reaches, so nothing triggered until the slider was
        #     dragged all the way left. Real inter-frame deltas sit around
        #     10-40, so 25 is the useful starting point.
        #   Blob Detection - a *minimum contour area* in pixels, where 127
        #     is a sensible floor. Changing the shared default would have
        #     silently altered blob behaviour too.
        self.threshold = 25 if tool_type in ("Motion Detection",
                                             "Delete (Missing Object)") else 127
        self.sensitivity = 50
        self.min_area = 100

        # Blob Properties
        self.blob_color = 0  # 0=Dark, 255=Light
        self.blob_max_area = 5000
        # How many blobs this tool expects to find. The score is "did I find
        # what I was looking for", so one blob against a target of one scores
        # 100. The old scoring was len(blobs) * 20, which meant a single blob
        # could only ever reach 20 and could never clear the default
        # sensitivity of 50 - the one-blob case was impossible to trigger.
        self.blob_target_count = 1
        # How the ROI is turned into a black/white mask before contours are
        # found:
        #   "Otsu"   - threshold chosen from the ROI's own histogram every
        #              frame. Survives lighting changes; the sane default.
        #   "Manual" - fixed level, for when Otsu picks badly (e.g. the ROI
        #              is almost entirely one shade).
        #   "Motion" - difference against the trained reference frame, so
        #              only what MOVED becomes a blob. This is what finds a
        #              part in a cluttered field of view: static background,
        #              however busy, subtracts away.
        self.blob_thresh_mode = "Otsu"
        self.blob_manual_thresh = 127
        self.blob_motion_thresh = 25
        # Speck removal before contours. Camera noise otherwise produces
        # dozens of tiny contours that swamp the real subject.
        self.blob_denoise = 5
        # Low-contrast handling. Otsu picks a level from the histogram, which
        # is right when subject and background are well separated but sits too
        # far toward the bright side when they are not. The bias shifts that
        # chosen level: NEGATIVE accepts dimmer blobs, positive demands
        # brighter ones. This is the "how dark a blob counts" control.
        self.blob_level_bias = 0
        # CLAHE before thresholding, for scenes where subject and background
        # are genuinely close in brightness. Off by default: it also amplifies
        # noise, so it is a deliberate trade rather than a free win.
        self.blob_contrast_boost = False
        self.last_blob_x, self.last_blob_y, self.last_blob_a = 0, 0, 0.0

        # LLM Properties
        self.llm_prompt = "Describe what is inside this image crop."
        # Turn a free-text answer into a pass/fail score. Ask the model to end
        # its reply with a sentinel when some condition holds ("...if a person
        # is present, end your reply with $ACTIVE"); seeing it drives the score
        # to 100, which is what makes an LLM tool able to gate a sequence the
        # same way a Motion or Pattern tool does.
        #
        # Mode matters more than it looks. "Ends with" is the default because
        # "Contains" misfires on a negative answer that quotes the sentinel
        # back - "no person, so I will not output $ACTIVE" contains $ACTIVE.
        self.activation_mode = "Off"        # Off | Ends with | Contains
        self.activation_token = "$ACTIVE"
        self.activation_hit = False         # last evaluation, for the UI
        self.llm_response = "Waiting for trigger..."
        self.llm_is_processing = False
        self.llm_trigger_time = 0.0
        self.llm_timeout_alarmed = False
        self.llm_generation = 0

        # State tracking (Raw vs Filtered)
        self.current_score = 0
        self.is_triggered = False
        self.raw_reference_frame = None;
        self.reference_frame = None
        self.raw_trained_template = None;
        self.trained_template = None

        # Motion/Delete tools: auto re-capture the reference frame once per
        # camera start instead of requiring a manual button press every time
        # the camera gets bumped out of position.
        # Image Capture tool: grabs a still from this tool's camera, either
        # cropped to the ROI box or the whole frame. Lives here rather than
        # on Studio's output canvas because capturing is camera work, and
        # Observatory is what owns the cameras and the ROI tooling.
        self.capture_mode = "ROI"      # "ROI" | "Full Frame"
        self.captured_path = ""
        self.capture_time = 0.0
        self.cutout_path = ""          # segmenter output, if run

        self.auto_capture_reference = False
        self.has_auto_captured = False

        # Filters
        self.use_thresh = False;
        self.filter_thresh = 127
        self.use_edge = False;
        self.filter_edge = 100
        self.use_depth = False;
        self.filter_depth = 5

        self.setPos(x, y)
        color_map = {"Delete (Missing Object)": "#e74c3c", "Motion Detection": "#3498db", "Blob Detection": "#9b59b6",
                     "Image Capture": "#f39c12",
                     "LLM Vision": "#1abc9c"}
        color = color_map.get(tool_type, "white")

        self.setPen(QPen(QColor(color), 2, Qt.PenStyle.DashLine))
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)

        self.handle = ROIResizeHandle(self, color)
        self.update_handle_pos()

        self.origin_handle = BlobOriginHandle(self) if self.tool_type == "Blob Detection" else None
        if self.origin_handle:
            # Default to the bottom-left corner of the ROI - a predictable,
            # sensible starting reference for X/Y output (standard bottom-up
            # coordinate convention) without requiring the user to drag it
            # into place first. Still fully repositionable afterward.
            self.origin_handle.setPos(0, self.rect().height())

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
        if self.origin_handle:
            self.origin_handle.setPos(min(self.origin_handle.pos().x(), w), min(self.origin_handle.pos().y(), h))

    def update_handle_pos(self):
        self.handle.setPos(self.rect().width() - 6, self.rect().height() - 6)

# Words that turn a sentence mentioning the sentinel into a refusal to emit
# it. Only ever consulted within the final clause - see _negated below.
_NEGATIONS = (" not ", "n't ", " no ", " never ", " without ", " cannot ",
              " refrain", " withhold", " omit", " skip", " unable")

_TRIM = " \t\r\n.,;:!?\"'`*_()[]{}"


def _negated(clause, token):
    """Does this clause *decline* to emit the token rather than emit it?

    Only the final clause is examined, so "there is no doubt a person is
    here. $ACTIVE" is not caught by the "no" three words earlier - that
    belongs to a different sentence.
    """
    before = clause.split(token)[0]
    return any(marker in f" {before} " for marker in _NEGATIONS)


def evaluate_activation(response, token, mode):
    """Did an LLM reply signal activation? None means "not evaluated".

    Matching is case-insensitive and tolerates trailing punctuation, quotes
    and markdown emphasis, because models routinely wrap a sentinel as
    `$ACTIVE.` or `**$ACTIVE**`.

    Modes, weakest guarantee last:

      "Exact reply" - the whole reply IS the token. Unambiguous, and what you
                      get by prompting "reply with only $ACTIVE, or only
                      $IDLE". Recommended when the decision actually matters.
      "Ends with"   - the reply's final clause ends with the token. Matches
                      the natural "...end your reply with $ACTIVE" phrasing,
                      with a negation guard, but see the caveat below.
      "Contains"    - the token appears anywhere. Loosest, and false-positives
                      on any reply that merely mentions the sentinel.

    Caveat worth knowing: "No person is present, so I will not output
    $ACTIVE." genuinely *ends with* the token, so pure string matching cannot
    separate it from a real activation. The negation guard catches that common
    phrasing, but it is a heuristic - "Exact reply" is the one with no
    ambiguity to guard against.
    """
    if not mode or mode == "Off" or not token:
        return None
    text = (response or "").strip().lower()
    tok = token.strip().lower()
    if not text or not tok:
        return False

    # Match the token's alphanumeric core, not the literal string. Observed
    # live on 2026-07-25: asked to "reply with only $ACTIVE", gemini-2.5-flash
    # answered "ACTIVE" - correct judgement, but it dropped the '$', which
    # models routinely do because it reads as markup. Literal matching scores
    # that correct answer as a miss.
    #
    # Word boundaries are kept so "$ACTIVE" still does NOT match "proactive".
    core = re.sub(r"[^a-z0-9_]", "", tok)
    if not core:
        return False

    if mode == "Contains":
        return re.search(rf"(?<![a-z0-9_]){re.escape(core)}(?![a-z0-9_])",
                         text) is not None

    if mode == "Exact reply":
        return re.fullmatch(rf"[^a-z0-9_]*{re.escape(core)}[^a-z0-9_]*",
                            text) is not None

    # "Ends with"
    tail = text.rstrip(_TRIM)
    if not re.search(rf"(?<![a-z0-9_]){re.escape(core)}[^a-z0-9_]*$", tail):
        return False
    # Look only at the last sentence/clause: a refusal reads as one sentence
    # ("... so I will not output $ACTIVE"), whereas a genuine activation
    # leaves the token standing alone after the explanation.
    last = tail
    for sep in (". ", "! ", "? ", "\n"):
        if sep in last:
            last = last.rsplit(sep, 1)[-1]
    return not _negated(last, tok)
