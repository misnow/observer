r"""
Which physical screen each output canvas lands on, and remembering it.

A projection setup is physical: canvas 0 goes to the projector over the bench,
canvas 1 to the one over the fixture. That mapping has to survive a restart, so
the program can be launched and put the right output on the right surface with
no fiddling.

    canvas 0  ->  \\.\DISPLAY2  (projector, fullscreen)
    canvas 1  ->  \\.\DISPLAY3  (projector, fullscreen)

THE RULE ABOUT MISSING SCREENS
------------------------------
If a projector isn't there - unplugged, powered off, not yet warmed up - the
canvas falls back to a normal window, exactly as it behaves today, and the
authored layout is untouched (the scene stays 1920x1080; only the window
presentation differs).

Crucially the SAVED CONFIGURATION IS NOT REWRITTEN. A fallback is a temporary
fact about this session, not a decision about the setup. Plug the projector
back in, relaunch, and the original assignment applies again. Only an explicit
override - `DisplayConfig.assign(..., forced=True)`, i.e. the user deliberately
changing it in the dialog - changes what is stored. Silently persisting a
fallback would quietly destroy a working configuration every time somebody
turned a projector off.

SCREEN IDENTITY
---------------
Screens are matched by NAME first (`\\.\DISPLAY2`), index second. Index alone
is fragile: Windows reorders screens when displays are added, removed or
re-detected, so a saved "screen 1" can silently become a different projector
between launches. The index is kept only as a fallback for when a name no
longer resolves.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout,
                             QLabel, QComboBox, QCheckBox, QPushButton,
                             QDialogButtonBox, QGroupBox, QFormLayout, QFrame)


SETTINGS_KEY = "canvas_display_config"

# Windowed fallback geometry - the same shape the app has always used when
# there was no second screen.
FALLBACK_GEOMETRY = (50, 50, 800, 600)


def list_screens():
    """[(index, name, description, geometry)] for every attached screen."""
    out = []
    primary = QApplication.primaryScreen()
    for i, screen in enumerate(QApplication.screens()):
        geo = screen.geometry()
        label = (f"{i}: {screen.name()}  {geo.width()}x{geo.height()}"
                 f"{'  (primary)' if screen is primary else ''}")
        out.append((i, screen.name(), label, geo))
    return out


def find_screen(name="", index=None):
    """Resolve a saved assignment to a live QScreen, or None.

    Name wins over index, because the index is only positional and shifts when
    displays are re-detected.
    """
    screens = QApplication.screens()
    if name:
        for screen in screens:
            if screen.name() == name:
                return screen
    if index is not None and 0 <= int(index) < len(screens):
        return screens[int(index)]
    return None


class Assignment:
    """Where one canvas wants to be."""

    def __init__(self, screen_name="", screen_index=None, fullscreen=True,
                 fit_mode="fit"):
        self.screen_name = str(screen_name or "")
        self.screen_index = None if screen_index is None else int(screen_index)
        self.fullscreen = bool(fullscreen)
        # How the 1920x1080 scene maps onto this screen - see
        # ProjectorCanvas.fit_scene for what each mode means.
        self.fit_mode = fit_mode if fit_mode in ("fit", "stretch", "actual") else "fit"

    def to_dict(self):
        return {"screen_name": self.screen_name,
                "screen_index": self.screen_index,
                "fullscreen": self.fullscreen,
                "fit_mode": self.fit_mode}

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(d.get("screen_name", ""), d.get("screen_index"),
                   d.get("fullscreen", True), d.get("fit_mode", "fit"))

    def __repr__(self):
        return (f"Assignment({self.screen_name or self.screen_index!r}, "
                f"fullscreen={self.fullscreen})")


class ApplyResult:
    """What actually happened when an assignment met reality."""

    def __init__(self, canvas_index, applied, screen_label="", reason=""):
        self.canvas_index = canvas_index
        self.applied = applied          # True = on its intended screen
        self.screen_label = screen_label
        self.reason = reason            # why it fell back, if it did

    @property
    def fell_back(self):
        return not self.applied

    def __repr__(self):
        return (f"ApplyResult(canvas {self.canvas_index}, "
                f"{'applied ' + self.screen_label if self.applied else 'FELL BACK: ' + self.reason})")


class DisplayConfig:
    """canvas index -> Assignment, persisted."""

    def __init__(self, assignments=None):
        self.assignments = dict(assignments or {})

    # --- editing ----------------------------------------------------------
    def assign(self, canvas_index, screen_name="", screen_index=None,
               fullscreen=True, forced=False, fit_mode="fit"):
        """Set where a canvas belongs.

        `forced` exists to make the intent explicit at the call site: only a
        deliberate user change should ever rewrite a stored assignment. Code
        reacting to a missing screen must NOT call this.
        """
        if not forced and canvas_index in self.assignments:
            return False
        self.assignments[int(canvas_index)] = Assignment(
            screen_name, screen_index, fullscreen, fit_mode)
        return True

    def clear(self, canvas_index):
        self.assignments.pop(int(canvas_index), None)

    def get(self, canvas_index):
        return self.assignments.get(int(canvas_index))

    # --- persistence ------------------------------------------------------
    def to_dict(self):
        return {str(k): v.to_dict() for k, v in sorted(self.assignments.items())}

    @classmethod
    def from_dict(cls, data):
        out = cls()
        for key, value in (data or {}).items():
            try:
                out.assignments[int(key)] = Assignment.from_dict(value)
            except (TypeError, ValueError):
                continue
        return out

    def save(self, settings):
        import json
        settings.setValue(SETTINGS_KEY, json.dumps(self.to_dict()))

    @classmethod
    def load(cls, settings):
        import json
        raw = settings.value(SETTINGS_KEY, "")
        if not raw:
            return cls()
        try:
            return cls.from_dict(json.loads(raw))
        except Exception:
            return cls()

    # --- the part that touches windows ------------------------------------
    def apply(self, canvases):
        """Place every canvas. Returns [ApplyResult], one per canvas.

        Never mutates the stored assignments - a canvas that cannot reach its
        screen falls back for this session only.
        """
        results = []
        for index, canvas in enumerate(canvases):
            assignment = self.get(index)
            if assignment is None:
                # Deliberately leaves the window ALONE rather than forcing it
                # windowed. ProjectorCanvas already auto-fullscreens onto a
                # second screen when one exists, and that long-standing
                # default must survive for anyone who never opens the setup
                # dialog. No assignment means "no opinion", not "windowed".
                results.append(ApplyResult(
                    index, False, reason="no screen assigned yet"))
                continue

            screen = find_screen(assignment.screen_name, assignment.screen_index)
            if screen is None:
                wanted = assignment.screen_name or f"index {assignment.screen_index}"
                results.append(ApplyResult(
                    index, False,
                    reason=f"screen {wanted} is not attached - showing in a "
                           f"window instead. The saved assignment is kept."))
                _show_windowed(canvas)
                continue

            setter = getattr(canvas, "set_fit_mode", None)
            if callable(setter):
                setter(assignment.fit_mode)
            _show_on_screen(canvas, screen, assignment.fullscreen)
            results.append(ApplyResult(index, True,
                                       screen_label=f"{screen.name()} "
                                                    f"{screen.geometry().width()}x"
                                                    f"{screen.geometry().height()}"))
        return results


def _show_windowed(canvas):
    """The long-standing no-second-screen behaviour."""
    try:
        canvas.showNormal()
        canvas.setGeometry(*FALLBACK_GEOMETRY)
        canvas.show()
    except RuntimeError:
        pass


def _show_on_screen(canvas, screen, fullscreen=True):
    """Move a canvas onto a specific screen and (optionally) fill it.

    setGeometry alone is not reliably enough on Windows for a window that is
    already mapped: the window handle keeps its original screen association,
    so showFullScreen() can fill the *old* monitor. Setting the handle's screen
    explicitly first is what makes it land on the intended one.
    """
    try:
        canvas.showNormal()
        canvas.setGeometry(screen.geometry())
        handle = canvas.windowHandle()
        if handle is not None:
            handle.setScreen(screen)
            canvas.setGeometry(screen.geometry())
        if fullscreen:
            # Borderless and filling the screen: a projector must show the
            # canvas and nothing else - no title bar, no frame, no chrome
            # stealing pixels from the projection area.
            canvas.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            canvas.showFullScreen()
        else:
            canvas.setWindowFlag(Qt.WindowType.FramelessWindowHint, False)
            canvas.show()
        fit = getattr(canvas, "fit_scene", None)
        if callable(fit):
            fit()
    except RuntimeError:
        pass


# --------------------------------------------------------------------------
class CanvasSetupDialog(QDialog):
    """Assign each output canvas to a screen.

    Lives here rather than in studio.py, which was split for being oversized;
    nothing in this module imports studio.
    """

    def __init__(self, canvas_count, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Canvas Setup - output screens")
        self.setMinimumWidth(560)
        self.config = DisplayConfig(dict(config.assignments))   # edit a copy
        self._rows = []

        root = QVBoxLayout(self)
        screens = list_screens()

        root.addWidget(QLabel(
            f"<b>{len(screens)} screen(s) detected.</b> Assign each output "
            f"canvas to the projector or monitor it should fill."))

        hint = QLabel(
            "<i>If an assigned screen isn't attached at launch, that canvas "
            "opens in a normal window instead and the authored layout is "
            "unchanged - the saved assignment is kept, so plugging the "
            "projector back in restores it. Only changing it here overrides "
            "what's stored.</i>")
        hint.setWordWrap(True)
        root.addWidget(hint)

        box = QGroupBox("Assignments")
        form = QFormLayout(box)
        for index in range(canvas_count):
            row = QHBoxLayout()
            combo = QComboBox()
            combo.addItem("(none - open in a window)", None)
            for i, name, label, _geo in screens:
                combo.addItem(label, name)

            existing = self.config.get(index)
            if existing is not None:
                pos = combo.findData(existing.screen_name)
                if pos < 0 and existing.screen_index is not None:
                    pos = existing.screen_index + 1     # +1 for the "(none)" row
                combo.setCurrentIndex(max(0, pos))

            full = QCheckBox("Fullscreen")
            full.setChecked(existing.fullscreen if existing else True)

            fit = QComboBox()
            for label, data in (("Fit (keep shape)", "fit"),
                                ("Stretch to fill", "stretch"),
                                ("Actual size 1:1", "actual")):
                fit.addItem(label, data)
            fpos = fit.findData(existing.fit_mode if existing else "fit")
            fit.setCurrentIndex(max(0, fpos))
            fit.setToolTip(
                "Fit: whole canvas visible, shape preserved - black bars if "
                "the screen is not 16:9.\n"
                "Stretch: fills the screen exactly, distorting on a non-16:9 "
                "screen.\n"
                "Actual size: no scaling at all (only sensible on a 1920x1080 "
                "screen).")

            row.addWidget(combo, 1)
            row.addWidget(fit)
            row.addWidget(full)
            holder = QFrame()
            holder.setLayout(row)
            form.addRow(f"Canvas {index + 1}:", holder)
            self._rows.append((index, combo, full, fit))
        root.addWidget(box)

        current = QGroupBox("Detected screens")
        cur_lay = QVBoxLayout(current)
        for _i, _name, label, geo in screens:
            cur_lay.addWidget(QLabel(f"  {label}   at ({geo.x()}, {geo.y()})"))
        root.addWidget(current)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply & Save")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def result_config(self):
        """The edited config. Everything here is a deliberate user choice, so
        it is written with forced=True."""
        out = DisplayConfig()
        for index, combo, full, fit in self._rows:
            name = combo.currentData()
            if name is None:
                continue
            screen_index = max(0, combo.currentIndex() - 1)
            out.assign(index, screen_name=name, screen_index=screen_index,
                       fullscreen=full.isChecked(), forced=True,
                       fit_mode=fit.currentData() or "fit")
        return out
