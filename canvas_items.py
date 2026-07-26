"""Canvas item classes for the Studio output canvas.

Split out of studio.py, which had grown to ~190KB. These are the placeable
assets a sequence step shows on a projection output canvas: text, images and
video, shapes, 3D models, Observatory captures, live HTML, and LLM responses.

Kept deliberately standalone, like vision_tools.py and ui_components.py:
nothing here imports studio, so the dependency runs one way only
(studio -> canvas_items). TriggerSettings lives here rather than with the
other sequence data classes because only these items ever construct it.

Two rules from CLAUDE.md apply directly to this file:

- This PyQt6 build escalates any unhandled Python exception raised inside a
  Qt-invoked slot to qFatal()/abort - exit code 0xC0000409, no traceback. Any
  callback that can arrive after its item is gone (worker completions in
  particular) must guard widget access with try/except RuntimeError. See
  Interactive3DModelItem._safe_update and its load worker.
- QThread references must be held in a list, never a single attribute: a
  second load would otherwise garbage-collect a still-running thread. See
  Interactive3DModelItem._loaders.
"""

import os
import time
import math

from PyQt6.QtWidgets import (QGraphicsItem, QGraphicsRectItem, QGraphicsTextItem,
                             QGraphicsPixmapItem, QGraphicsProxyWidget)
from PyQt6.QtCore import Qt, QUrl, QSizeF, QRectF, QPointF
from PyQt6.QtGui import (QColor, QBrush, QFont, QPen, QPixmap, QPainterPath, QPainter,
                         QFontMetricsF, QTransform, QPolygonF)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
from PyQt6.QtSvg import QSvgRenderer

from vision_tools import ROIResizeHandle


class TriggerSettings:
    def __init__(self):
        self.wait_type = "Line"
        self.observatory_file = ""
        self.vision_target = "None"
        self.condition = "Is True"

class AnimatableMixin:
    """Shared entrance/ambient animation behavior, mixed into Text, Media,
    and Shape items so all three reuse one implementation instead of
    duplicating it three times. Deliberately NOT a full keyframe timeline -
    a handful of canned motions driven by the existing 100ms engine tick,
    matching how "Blink" already works: animation only actually plays while
    the sequence engine is running a step this item belongs to, never
    during static Design Mode editing.
    """

    def _init_animation(self):
        self.animation_type = "None"       # None | Fade In | Slide In | Pulse | Pan
        self.animation_direction = "Left"  # Left | Right | Top | Bottom (Slide In / Pan)
        self.animation_duration = 1.0       # seconds
        self.anim_start_time = 0.0
        self._anim_base_pos = None
        # Scale is always applied via QTransform (never setScale()) so locked
        # (scale_x == scale_y, driven by scale_factor) and unlocked
        # (independent scale_x/scale_y, skewing e.g. a square into a
        # rectangle) both go through one consistent code path instead of two
        # composing/conflicting transform systems.
        self.lock_aspect_ratio = True
        self.scale_factor = 1.0
        self.scale_x = 1.0
        self.scale_y = 1.0
        self.output_canvas = 0  # which output canvas this asset renders on

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemVisibleHasChanged:
            if value:
                self.anim_start_time = time.time()
                self._anim_base_pos = self.pos()
            elif self._anim_base_pos is not None:
                # Returning to a clean, "as authored" resting state on hide
                # means Design Mode never shows a random mid-animation frame,
                # and a later replay always starts from the same baseline
                # instead of drifting further each loop.
                self.setPos(self._anim_base_pos)
                self.setOpacity(1.0)
                self._apply_scale_transform()
        return super().itemChange(change, value)

    def _apply_scale_transform(self):
        self.setTransformOriginPoint(self.boundingRect().center())
        self.setTransform(QTransform().scale(self.scale_x, self.scale_y))

    def set_scale_factor(self, factor):
        self.scale_factor = factor
        self.scale_x = factor
        self.scale_y = factor
        self._apply_scale_transform()

    def set_scale_xy(self, sx, sy):
        self.scale_x = sx
        self.scale_y = sy
        self._apply_scale_transform()

    def set_lock_aspect_ratio(self, locked):
        self.lock_aspect_ratio = locked
        if locked:
            # Re-locking snaps back to one uniform scale, discarding any
            # skew - the average of the last independent x/y scales is a
            # reasonable, predictable value to settle on.
            self.set_scale_factor((self.scale_x + self.scale_y) / 2.0)

    def reset_scale_rotation_animation(self):
        """Shared portion of "Reset to Default": scale/rotation/lock/blink/
        animation. Each item class adds its own type-specific fields
        (font/color for Text, outline/fill for Shape, etc.) on top."""
        self.lock_aspect_ratio = True
        self.set_scale_factor(1.0)
        self.setRotation(0)
        self.setOpacity(1.0)
        self.is_blinking = False
        self.animation_type = "None"
        self.animation_direction = "Left"
        self.animation_duration = 1.0

    def set_animation_type(self, anim_type):
        self.animation_type = anim_type
        self.anim_start_time = time.time()

    def set_animation_direction(self, direction):
        self.animation_direction = direction

    def set_animation_duration(self, duration):
        self.animation_duration = max(0.1, duration)

    def update_animation(self):
        if self.animation_type == "None" or not self.isVisible() or self._anim_base_pos is None:
            return

        elapsed = time.time() - self.anim_start_time
        duration = max(0.1, self.animation_duration)
        t = min(1.0, elapsed / duration)
        eased = 1 - (1 - t) ** 3  # ease-out cubic, for the one-shot entrances

        if self.animation_type == "Fade In":
            self.setOpacity(eased)

        elif self.animation_type == "Slide In":
            offset = 300 * (1 - eased)
            dx, dy = {
                "Left": (-offset, 0), "Right": (offset, 0),
                "Top": (0, -offset), "Bottom": (0, offset),
            }.get(self.animation_direction, (-offset, 0))
            self.setPos(self._anim_base_pos.x() + dx, self._anim_base_pos.y() + dy)
            self.setOpacity(eased)

        elif self.animation_type == "Pulse":
            # Temporarily bulges the CURRENT scale_x/scale_y (whatever they
            # are, locked or skewed) without overwriting them - the shape's
            # actual configured aspect ratio is preserved once the pulse
            # settles back to phase 0.
            phase = (elapsed % duration) / duration
            breathe = 1 + 0.08 * math.sin(2 * math.pi * phase)
            self.setTransformOriginPoint(self.boundingRect().center())
            self.setTransform(QTransform().scale(self.scale_x * breathe, self.scale_y * breathe))

        elif self.animation_type == "Pan":
            phase = (elapsed % duration) / duration
            amplitude = 60
            offset = amplitude * math.sin(2 * math.pi * phase)
            dx, dy = {
                "Left": (offset, 0), "Right": (-offset, 0),
                "Top": (0, offset), "Bottom": (0, -offset),
            }.get(self.animation_direction, (offset, 0))
            self.setPos(self._anim_base_pos.x() + dx, self._anim_base_pos.y() + dy)

class InteractiveTextItem(AnimatableMixin, QGraphicsTextItem):
    def __init__(self, text="New Text"):
        super().__init__(text)
        self.trigger = TriggerSettings()
        self._init_animation()
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.current_font_size = 48
        self.is_blinking = False
        # 0 = normal straight text (Qt's own layout/rendering, via
        # super().paint()). Nonzero switches to manual per-character
        # painting along a circular arc of this radius; sign picks which
        # way the text bows (positive = arches up like a rainbow, negative
        # = wraps under like the bottom of a dome).
        self.curve_radius = 0
        self.setFont(QFont("Arial", self.current_font_size))
        self.text_color = QColor("white")
        self.setDefaultTextColor(self.text_color)

        self.setTransformOriginPoint(self.boundingRect().center())
        self.handle = MediaResizeHandle(self)
        self._reposition_handle()

    def set_text_color(self, color):
        self.text_color = color
        self.setDefaultTextColor(color)
        self.update()

    def set_curve_radius(self, radius):
        self.curve_radius = radius
        self._reposition_handle()
        self.update()

    def setPlainText(self, text):
        super().setPlainText(text)
        if hasattr(self, 'handle'):
            self._reposition_handle()

    def setFont(self, font):
        super().setFont(font)
        if hasattr(self, 'handle'):
            self._reposition_handle()

    def resize_by_drag(self, x, y):
        base_w, base_h = self.boundingRect().width(), self.boundingRect().height()
        if base_w <= 0 or base_h <= 0:
            return
        if self.lock_aspect_ratio:
            factor = max(x / base_w, y / base_h)
            self.set_scale_factor(max(0.2, min(5.0, factor)))
        else:
            self.set_scale_xy(max(0.2, min(5.0, x / base_w)), max(0.2, min(5.0, y / base_h)))

    def reset_to_default(self):
        self.reset_scale_rotation_animation()
        self.current_font_size = 48
        self.setFont(QFont("Arial", 48))
        self.set_text_color(QColor("white"))
        self.curve_radius = 0
        self._reposition_handle()

    def boundingRect(self):
        if self.curve_radius == 0:
            return super().boundingRect()
        # A generous square superset of the arc's extent is enough here -
        # QGraphicsItem only needs boundingRect() to contain everything
        # paint() draws, not to be pixel-tight.
        abs_r = max(abs(self.curve_radius), 20)
        margin = QFontMetricsF(self.font()).height() + 10
        size = abs_r + margin
        return QRectF(-size, -size, size * 2, size * 2)

    def paint(self, painter, option, widget=None):
        if self.curve_radius == 0:
            super().paint(painter, option, widget)
            return

        text = self.toPlainText()
        if not text:
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        font = self.font()
        painter.setFont(font)
        painter.setPen(QPen(self.text_color))

        radius = self.curve_radius
        abs_r = max(abs(radius), 20)
        sign = 1 if radius > 0 else -1
        fm = QFontMetricsF(font)
        char_height = fm.height()
        widths = [fm.horizontalAdvance(ch) for ch in text]
        total_angle = sum(w / abs_r for w in widths)

        theta = -total_angle / 2.0
        for ch, cw in zip(text, widths):
            char_angle = cw / abs_r
            mid_theta = theta + char_angle / 2.0
            x = abs_r * math.sin(mid_theta)
            y = sign * abs_r * (1 - math.cos(mid_theta))
            rot_deg = sign * math.degrees(mid_theta)

            painter.save()
            painter.translate(x, y)
            painter.rotate(rot_deg)
            painter.drawText(QRectF(-cw / 2.0, -char_height, cw, char_height),
                              Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, ch)
            painter.restore()
            theta += char_angle

    def _reposition_handle(self):
        rect = self.boundingRect()
        self.setTransformOriginPoint(rect.center())
        self.handle.setPos(rect.width() - 6, rect.height() - 6)

class MediaResizeHandle(QGraphicsRectItem):
    """Bottom-right corner drag handle for InteractiveMediaItem. Unlike
    ROIResizeHandle (which resizes a rect's width/height independently),
    this computes a single uniform scale factor so images/video don't get
    stretched out of their aspect ratio."""

    def __init__(self, parent):
        super().__init__(0, 0, 12, 12, parent)
        self.setBrush(QBrush(QColor("#3498db")))
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
            self.parentItem().resize_by_drag(pos_in_parent.x(), pos_in_parent.y())
            event.accept()

    def mouseReleaseEvent(self, event):
        self._dragging = False
        event.accept()

class InteractiveMediaItem(AnimatableMixin, QGraphicsRectItem):
    def __init__(self, filepath="Placeholder", media_type="Image"):
        super().__init__(0, 0, 200, 150)
        self.trigger = TriggerSettings()
        self._init_animation()
        self.filepath = filepath
        self.media_type = media_type
        self.video_item = None
        self.media_player = None
        self.audio_output = None
        self.is_video_playing = False
        self.time_hold = False
        self.is_blinking = False
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)

        if media_type == "Image" and os.path.exists(filepath):
            pixmap = QPixmap(filepath).scaled(400, 400, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation)
            self.pixmap_item = QGraphicsPixmapItem(pixmap, self)
            self.setRect(self.pixmap_item.boundingRect())
            self.setPen(QPen(Qt.PenStyle.NoPen))
        else:
            color_hex, icon = "#3498db", "🎬" if media_type == "Video" else "🎨"
            self.setPen(QPen(QColor(color_hex), 2))
            self.setBrush(QBrush(QColor(40, 40, 40, 200)))
            self.text_tag = QGraphicsTextItem(f"{icon} {os.path.basename(filepath)}", self)
            self.text_tag.setDefaultTextColor(QColor("white"))
            self.text_tag.setPos(5, 5)

        self.base_width = self.rect().width()
        self.base_height = self.rect().height()
        self.setTransformOriginPoint(self.rect().center())

        self.handle = MediaResizeHandle(self)
        self.handle.setPos(self.base_width - 6, self.base_height - 6)

        if media_type == "Video" and os.path.exists(filepath):
            self.video_item = QGraphicsVideoItem(self)
            self.video_item.setSize(QSizeF(self.base_width, self.base_height))
            self.video_item.setVisible(False)
            self.media_player = QMediaPlayer()
            self.audio_output = QAudioOutput()
            self.media_player.setAudioOutput(self.audio_output)
            self.media_player.setVideoOutput(self.video_item)
            self.media_player.setSource(QUrl.fromLocalFile(filepath))

    def resize_by_drag(self, x, y):
        # Video keeps uniform scaling regardless of the lock flag - skewing
        # a playing video looks broken and isn't something we want even if
        # a loaded project file somehow has lock_aspect_ratio=False on one.
        if self.lock_aspect_ratio or self.media_type == "Video":
            factor = max(x / self.base_width, y / self.base_height)
            self.set_scale_factor(max(0.2, min(5.0, factor)))
        else:
            self.set_scale_xy(max(0.2, min(5.0, x / self.base_width)),
                              max(0.2, min(5.0, y / self.base_height)))

    def reset_to_default(self):
        self.reset_scale_rotation_animation()
        self.time_hold = False

    def play_from_start(self):
        if not self.media_player:
            return
        if hasattr(self, 'text_tag'): self.text_tag.setVisible(False)
        self.video_item.setVisible(True)
        self.media_player.setPosition(0)
        self.media_player.play()
        self.is_video_playing = True

    def stop_video(self):
        # Hiding the graphics item only stops it from being drawn - the
        # QMediaPlayer keeps decoding and playing audio in the background
        # unless explicitly told to stop.
        if self.media_player:
            self.media_player.stop()
            self.is_video_playing = False

    def mouseDoubleClickEvent(self, event):
        if self.media_type == "Video" and self.media_player:
            if not self.is_video_playing:
                self.play_from_start()
            elif self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.media_player.pause()
            else:
                self.media_player.play()
        super().mouseDoubleClickEvent(event)

class InteractiveShapeItem(AnimatableMixin, QGraphicsItem):
    """A placed instance from the shape template library. "Square"/"Circle"/
    "Triangle" are drawn procedurally so their outline thickness and
    stroke/fill colors stay fully parametric per instance; "Custom SVG"
    renders an arbitrary .svg file (e.g. from shape_library/) as-is via
    QSvgRenderer, respecting that file's own transparency/colors."""

    def __init__(self, shape_type="Square", svg_path=""):
        super().__init__()
        self.trigger = TriggerSettings()
        self._init_animation()
        self.shape_type = shape_type
        self.svg_path = svg_path
        self.base_size = 200.0
        self.outline_width = 4
        self.outline_color = QColor("#ffffff")
        self.fill_enabled = False
        self.fill_color = QColor("#3498db")
        self.is_blinking = False

        self._svg_renderer = None
        if self.shape_type == "Custom SVG" and svg_path and os.path.exists(svg_path):
            self._svg_renderer = QSvgRenderer(svg_path)

        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setTransformOriginPoint(self.base_size / 2, self.base_size / 2)
        self.handle = MediaResizeHandle(self)
        self.handle.setPos(self.base_size - 6, self.base_size - 6)

    def boundingRect(self):
        return QRectF(0, 0, self.base_size, self.base_size)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self.shape_type == "Custom SVG":
            if self._svg_renderer and self._svg_renderer.isValid():
                self._svg_renderer.render(painter, self.boundingRect())
            else:
                painter.setPen(QPen(QColor("#e74c3c"), 2, Qt.PenStyle.DashLine))
                painter.drawRect(self.boundingRect())
                painter.drawText(self.boundingRect(), Qt.AlignmentFlag.AlignCenter, "Missing SVG")
            return

        painter.setPen(QPen(self.outline_color, max(1, self.outline_width)))
        painter.setBrush(QBrush(self.fill_color) if self.fill_enabled else Qt.BrushStyle.NoBrush)

        margin = max(1, self.outline_width) / 2.0
        rect = self.boundingRect().adjusted(margin, margin, -margin, -margin)

        if self.shape_type == "Square":
            painter.drawRect(rect)
        elif self.shape_type == "Circle":
            painter.drawEllipse(rect)
        elif self.shape_type == "Triangle":
            path = QPainterPath()
            path.moveTo(rect.center().x(), rect.top())
            path.lineTo(rect.right(), rect.bottom())
            path.lineTo(rect.left(), rect.bottom())
            path.closeSubpath()
            painter.drawPath(path)

    def set_shape_svg(self, svg_path):
        self.svg_path = svg_path
        self._svg_renderer = QSvgRenderer(svg_path) if svg_path and os.path.exists(svg_path) else None
        self.update()

    def set_outline_width(self, width):
        self.outline_width = width
        self.update()

    def set_outline_color(self, color):
        self.outline_color = color
        self.update()

    def set_fill_enabled(self, enabled):
        self.fill_enabled = enabled
        self.update()

    def set_fill_color(self, color):
        self.fill_color = color
        self.update()

    def resize_by_drag(self, x, y):
        if self.lock_aspect_ratio:
            factor = max(x / self.base_size, y / self.base_size)
            self.set_scale_factor(max(0.2, min(5.0, factor)))
        else:
            self.set_scale_xy(max(0.2, min(5.0, x / self.base_size)),
                              max(0.2, min(5.0, y / self.base_size)))

    def reset_to_default(self):
        self.reset_scale_rotation_animation()
        self.outline_width = 4
        self.outline_color = QColor("#ffffff")
        self.fill_enabled = False
        self.fill_color = QColor("#3498db")
        self.update()

class Interactive3DModelItem(AnimatableMixin, QGraphicsRectItem):
    """A 3D model (.stl/.obj/.ply/.glb or .step/.stp) rendered on the canvas.

    Rendering is software (numpy + QPainter, see model3d.py) rather than
    OpenGL: this machine has no usable GPU, and Qt's GPU-adjacent widgets
    are what have repeatedly crashed this app.

    Loading runs on a worker thread because STEP import cost scales with
    face count - a face-heavy faceted STEP measured at ~100 seconds - and a
    freeze that long is indistinguishable from a hang.
    """

    def __init__(self, filepath="", width=480, height=360):
        super().__init__(0, 0, width, height)
        self.trigger = TriggerSettings()
        self._init_animation()
        self.output_canvas = 0
        self.filepath = filepath
        self.is_blinking = False

        # Model pose, kept separate from the QGraphicsItem's own position and
        # scale so the 2D placement of the viewport and the 3D pose of the
        # model inside it stay independent.
        self.rot_x = 20.0
        self.rot_y = -30.0
        self.rot_z = 0.0
        self.model_scale = 1.0
        self.model_tx = 0.0
        self.model_ty = 0.0

        self.vertices = None
        self.faces = None
        self.load_error = ""
        self.is_loading = False
        self._loaders = []
        self._load_generation = 0
        self._home_pos = None
        self._drag_last = None

        self.model_color = QColor("#4aa3df")
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setPen(QPen(QColor("#4aa3df"), 2))
        self.setBrush(QBrush(QColor(18, 18, 22, 235)))
        self.handle = MediaResizeHandle(self)
        self.handle.setPos(width - 6, height - 6)

        if filepath:
            self.load_model_async(filepath)

    # --- loading ---------------------------------------------------------
    def load_model_async(self, filepath, on_done=None):
        import model3d
        self.filepath = filepath
        self.is_loading = True
        self.load_error = ""
        self._safe_update()

        worker = model3d.ModelLoadWorker(filepath)
        self._load_generation += 1
        generation = self._load_generation

        def handle(vertices, faces, error, gen=generation, wk=worker):
            # Drop results from a superseded load: browsing to a second file
            # while a slow STEP import is still running would otherwise let
            # the older result overwrite the newer one when it finishes.
            if gen != self._load_generation:
                self._retire_loader(wk)
                return
            self.is_loading = False
            if error:
                self.load_error = error
                self.vertices = self.faces = None
            else:
                self.vertices, self.faces = vertices, faces
                self.load_error = ""
            # Queued cross-thread slot: by the time it arrives the item may
            # have been deleted (project loaded, canvas removed). Touching a
            # dead C++ object raises RuntimeError, and an exception escaping
            # a Qt slot is escalated by this PyQt6 build into a hard abort.
            self._safe_update()
            if on_done:
                try:
                    on_done(error)
                except RuntimeError:
                    pass
            self._retire_loader(wk)

        worker.loaded.connect(handle)
        # A list, not a single attribute: a second load must not replace -
        # and thereby garbage collect - a QThread that is still running.
        self._loaders.append(worker)
        worker.start()

    def _retire_loader(self, worker):
        try:
            if worker in self._loaders:
                self._loaders.remove(worker)
        except Exception:
            pass

    def _safe_update(self):
        try:
            self.update()
        except RuntimeError:
            pass   # underlying C++ item already destroyed

    def face_count(self):
        return 0 if self.faces is None else len(self.faces)

    # --- 3D transform ----------------------------------------------------
    def set_rotation_3d(self, rx=None, ry=None, rz=None):
        if rx is not None: self.rot_x = float(rx)
        if ry is not None: self.rot_y = float(ry)
        if rz is not None: self.rot_z = float(rz)
        self.update()

    def set_model_scale(self, s):
        self.model_scale = max(0.05, float(s))
        self.update()

    def set_model_translation(self, tx=None, ty=None):
        if tx is not None: self.model_tx = float(tx)
        if ty is not None: self.model_ty = float(ty)
        self.update()

    # --- canvas reference point ------------------------------------------
    def home_position(self):
        """Canvas position the X/Y readout is measured from."""
        if self._home_pos is None:
            self._home_pos = self.pos()
        return self._home_pos

    def set_home_position(self, pos=None):
        self._home_pos = QPointF(pos) if pos is not None else self.pos()

    def canvas_offset(self):
        home = self.home_position()
        return self.pos().x() - home.x(), self.pos().y() - home.y()

    def reset_view(self):
        self.rot_x, self.rot_y, self.rot_z = 20.0, -30.0, 0.0
        self.model_scale = 1.0
        self.model_tx = self.model_ty = 0.0
        # Return the box to its reference point too, so the readout goes back
        # to 0,0 - the same origin the numbers are measured from.
        self.setPos(self.home_position())
        self.update()

    def reset_to_default(self):
        self.reset_scale_rotation_animation()
        self.reset_view()

    def resize_by_drag(self, x, y):
        w = max(200.0, min(2000.0, x))
        h = max(150.0, min(2000.0, y))
        self.setRect(0, 0, w, h)
        self.handle.setPos(w - 6, h - 6)
        self.update()

    # --- interaction -----------------------------------------------------
    def mousePressEvent(self, event):
        # Ctrl or Shift + drag repositions the whole viewport box instead of
        # orbiting the model inside it. Without a modifier there'd be no way
        # to move this item at all: a plain left-press is consumed by the
        # orbit handler.
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                                Qt.KeyboardModifier.ShiftModifier):
            self._drag_last = None
            super().mousePressEvent(event)   # Qt's own ItemIsMovable handling
            return
        if event.button() == Qt.MouseButton.LeftButton and self.faces is not None:
            self._drag_last = event.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Left-drag orbits. The pivot is wherever you pressed: translation is
        # compensated each step so the grabbed point stays put on screen
        # instead of sliding out from under the cursor.
        if self._drag_last is not None:
            delta = event.pos() - self._drag_last
            self._drag_last = event.pos()

            rect = self.rect()
            pivot_x = event.pos().x() - (rect.width() / 2.0 + self.model_tx)
            pivot_y = event.pos().y() - (rect.height() / 2.0 + self.model_ty)

            d_yaw = delta.x() * 0.5
            d_pitch = delta.y() * 0.5
            self.rot_y += d_yaw
            self.rot_x += d_pitch

            ang = math.radians(d_yaw)
            ca, sa = math.cos(ang), math.sin(ang)
            new_px = pivot_x * ca - pivot_y * sa
            new_py = pivot_x * sa + pivot_y * ca
            self.model_tx += pivot_x - new_px
            self.model_ty += pivot_y - new_py

            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_last is not None:
            self._drag_last = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        self.set_model_scale(self.model_scale * (1.1 if event.delta() > 0 else 1 / 1.1))
        event.accept()

    # --- rendering -------------------------------------------------------
    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        rect = self.rect()
        painter.save()
        painter.setClipRect(rect)

        if self.is_loading:
            painter.setPen(QPen(QColor("#f39c12")))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "Loading model...\n(STEP files with many faces can take a while)")
        elif self.load_error:
            painter.setPen(QPen(QColor("#e74c3c")))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"Load failed:\n{self.load_error}")
        elif self.faces is None or len(self.faces) == 0:
            painter.setPen(QPen(QColor("#888888")))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "No model loaded\nUse Browse in the properties panel")
        else:
            import model3d
            polys, shades, _ = model3d.project(
                self.vertices, self.faces, self.rot_x, self.rot_y, self.rot_z,
                self.model_scale, self.model_tx, self.model_ty,
                rect.width(), rect.height())
            base = self.model_color
            painter.setPen(Qt.PenStyle.NoPen)
            for tri, shade in zip(polys, shades):
                painter.setBrush(QBrush(QColor(int(base.red() * shade),
                                               int(base.green() * shade),
                                               int(base.blue() * shade))))
                painter.drawPolygon(QPolygonF([QPointF(float(p[0]), float(p[1])) for p in tri]))

        painter.restore()


class InteractiveCaptureItem(AnimatableMixin, QGraphicsRectItem):
    """Displays the still held by an Observatory "Image Capture" tool.

    This item does NOT capture anything itself and owns no ROI. The capture
    and its region live in Observatory, which is what owns the cameras and
    the ROI tooling; putting an ROI here previously dropped a selection box
    onto the projection output canvas, which was never the intent.

    So this is a *reference*, following exactly the pattern LLM Call uses:
    pick an Observatory project file, pick a tool from it, and this shows
    whatever that tool most recently captured.
    """

    def __init__(self, item_name="Capture 1", observatory_file="", source_tool="None"):
        super().__init__(0, 0, 480, 360)
        self.trigger = TriggerSettings()
        self._init_animation()
        self.item_name = item_name
        self.observatory_file = observatory_file
        # Name of the Observatory Image Capture tool this mirrors.
        self.source_tool = source_tool
        self.image_path = ""
        self.last_shown_time = 0.0
        # Transparent-background cutout produced by the segmenter, if run.
        self.cutout_path = ""
        # Which of the source tool's two images to mirror: the raw capture,
        # or the transparent cutout the segmenter produced from it. Both are
        # published by Observatory; this picks which one lands on the canvas.
        self.source_variant = "Captured"
        self.is_blinking = False

        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setPen(QPen(QColor("#f39c12"), 2))
        self.setBrush(QBrush(QColor(30, 30, 30, 220)))

        self.pixmap_item = QGraphicsPixmapItem(self)
        self.label = QGraphicsTextItem(f"📷 {item_name}", self)
        self.label.setDefaultTextColor(QColor("#f39c12"))
        self.label.setPos(6, 2)
        self.status = QGraphicsTextItem("No capture yet - double-click to capture", self)
        self.status.setDefaultTextColor(QColor("#888888"))
        self.status.setPos(6, 26)

        self.handle = MediaResizeHandle(self)
        self.handle.setPos(self.rect().width() - 6, self.rect().height() - 6)

    def set_item_name(self, name):
        self.item_name = name
        self.label.setPlainText(f"📷 {name}")

    def load_capture(self, path):
        """Display a captured PNG, sizing this item to the image."""
        if not path or not os.path.exists(path):
            return False
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return False
        self.image_path = path
        self.pixmap_item.setPixmap(pixmap)
        self.setRect(0, 0, pixmap.width(), pixmap.height())
        self.handle.setPos(pixmap.width() - 6, pixmap.height() - 6)
        self.status.setPlainText("")
        return True


    def resize_by_drag(self, x, y):
        if self.lock_aspect_ratio:
            base_w = max(1.0, self.rect().width())
            base_h = max(1.0, self.rect().height())
            factor = max(x / base_w, y / base_h)
            self.set_scale_factor(max(0.2, min(5.0, factor)))
        else:
            self.set_scale_xy(max(0.2, min(5.0, x / max(1.0, self.rect().width()))),
                              max(0.2, min(5.0, y / max(1.0, self.rect().height()))))

    def reset_to_default(self):
        self.reset_scale_rotation_animation()


class InteractiveHTMLItem(QGraphicsProxyWidget):
    """A movable/resizable window on the Output Canvas hosting a real,
    JS-capable QWebEngineView. This is the one place in the app that embeds
    a live focusable widget inside the graphics scene via QGraphicsProxyWidget
    - the same pattern that was previously identified as this app's most
    reliable crash source (it corrupted Qt's modal-dialog/focus handling the
    moment a QFileDialog opened elsewhere). That prior offender was removed
    entirely; this one exists because the user explicitly chose real-webpage
    capability over the safer static-HTML-render alternative, understanding
    the risk. Resizing calls setFixedSize() on the actual QWebEngineView
    (re-rendering at the new size) rather than a scale transform, so text
    stays crisp instead of blurring."""

    def __init__(self, url=""):
        super().__init__()
        # Imported lazily (not at module top) so merely importing this
        # module never imposes WebEngine's "must set AA_ShareOpenGLContexts
        # before QApplication is created" requirement on unrelated callers -
        # only actually constructing an HTML item does, and main() already
        # sets that attribute before creating its QApplication.
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        self.trigger = TriggerSettings()
        self.output_canvas = 0
        self.url = url
        self.base_width = 480
        self.base_height = 360

        self.web_view = QWebEngineView()
        self.web_view.setFixedSize(self.base_width, self.base_height)
        self.setWidget(self.web_view)
        if url:
            self.load_url(url)
        else:
            self.web_view.setHtml("<body style='background:#222;color:#888;"
                                   "font-family:sans-serif;display:flex;align-items:center;"
                                   "justify-content:center;'>No URL loaded</body>")

        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.handle = MediaResizeHandle(self)
        self.handle.setPos(self.base_width - 6, self.base_height - 6)

    def load_url(self, url):
        self.url = url
        if os.path.exists(url):
            self.web_view.load(QUrl.fromLocalFile(url))
        else:
            target = url if "://" in url else f"https://{url}"
            self.web_view.load(QUrl(target))

    def resize_by_drag(self, x, y):
        w = int(max(160, min(2000, x)))
        h = int(max(120, min(2000, y)))
        self.base_width, self.base_height = w, h
        # QGraphicsProxyWidget resets its own pos() to (0,0) as a side
        # effect of the embedded widget's setFixedSize() - save/restore
        # around it so resizing never silently relocates the item.
        current_pos = self.pos()
        self.web_view.setFixedSize(w, h)
        self.setPos(current_pos)
        self.handle.setPos(w - 6, h - 6)

class InteractiveLLMTextItem(QGraphicsRectItem):
    def __init__(self, tool_name="LLM Call", observatory_file="", source_tool="None",
                 prompt="Describe what is inside this image crop."):
        super().__init__(0, 0, 420, 220)
        self.trigger = TriggerSettings()
        self.output_canvas = 0
        self.tool_name = tool_name
        self.observatory_file = observatory_file
        self.source_tool = source_tool
        self.prompt = prompt
        self.response = ""
        self.current_font_size = 16
        self.text_color = QColor("#00ff99")
        self.scroll_offset = 0.0

        # "Hold for Response": block step advancement until a fresh response
        # (one that arrived after THIS trigger, not a stale one from a
        # previous loop iteration) comes back, with a failsafe timeout so a
        # dead/misconfigured tool can't hang the sequence forever.
        self.hold_for_response = False
        self.response_pending = False
        self.trigger_sent_time = 0
        self.failsafe_triggered = False

        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setPen(QPen(QColor("#1abc9c"), 2, Qt.PenStyle.DashLine))
        self.setBrush(QBrush(QColor(20, 20, 20, 220)))
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)

        self.label = QGraphicsTextItem(f"🧠 {tool_name}", self)
        self.label.setDefaultTextColor(QColor("#1abc9c"))
        self.label.setPos(6, 2)

        # A clipped "viewport" rect item with a text child, instead of a native
        # QTextBrowser embedded via QGraphicsProxyWidget: that combination is the
        # only spot in the app that embeds a real focusable widget inside the
        # graphics scene, and it corrupts Qt's modal-dialog/focus handling and
        # crashes the whole process the moment any file dialog opens elsewhere.
        # Plain QGraphicsItem primitives (used everywhere else in this app) don't
        # have that problem, so scrolling/wrapping is done by hand here instead.
        self.viewport = QGraphicsRectItem(self)
        self.viewport.setPen(QPen(Qt.PenStyle.NoPen))
        self.viewport.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.viewport.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape, True)

        self.content = QGraphicsTextItem("Waiting for Observatory response...", self.viewport)
        self.content.setDefaultTextColor(self.text_color)

        self.handle = ROIResizeHandle(self, "#1abc9c")
        self._apply_style()
        self._reposition_children()

    def _apply_style(self):
        self.content.setFont(QFont("Consolas", self.current_font_size))
        self.content.setDefaultTextColor(self.text_color)

    def set_font_size(self, size):
        self.current_font_size = size
        self._apply_style()
        self._apply_scroll()

    def set_text_color(self, color):
        self.text_color = color
        self._apply_style()

    def set_tool_name(self, name):
        self.tool_name = name
        self.label.setPlainText(f"🧠 {name}")

    def set_response(self, text):
        self.response = text
        self.content.setPlainText(text)
        self.scroll_offset = 0.0
        self._apply_scroll()

    def wheelEvent(self, event):
        self.scroll_offset -= event.delta() / 2.0
        self._apply_scroll()
        event.accept()

    def _apply_scroll(self):
        viewport_h = self.viewport.rect().height()
        content_h = self.content.boundingRect().height()
        max_scroll = max(0.0, content_h - viewport_h)
        self.scroll_offset = max(0.0, min(self.scroll_offset, max_scroll))
        self.content.setPos(0, -self.scroll_offset)

    def update_size(self, w, h):
        w, h = max(220, w), max(120, h)
        self.setRect(0, 0, w, h)
        self._reposition_children()

    def _reposition_children(self):
        rect = self.rect()
        self.viewport.setRect(0, 0, rect.width() - 12, rect.height() - 34)
        self.viewport.setPos(6, 26)
        self.content.setTextWidth(rect.width() - 12)
        self.handle.setPos(rect.width() - 6, rect.height() - 6)
        self._apply_scroll()
