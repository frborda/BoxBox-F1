"""Paneles desacoplables: cualquier widget envuelto en `Detachable` gana una
barrita de título con botones para desacoplarlo a una ventana propia (con
marco nativo: se mueve y redimensiona libremente), fijarlo (sin marco,
siempre encima e inamovible), capturarlo en imagen (📷: clipboard + guardar),
ocultarlo y volver a acoplarlo donde estaba.

Colocación precisa para armar mosaicos sin huecos:
- IMÁN: al mover o redimensionar cerca de otra ventana de la app (o del
  borde de pantalla), los bordes se pegan solos (≤ 12 px).
- Teclado sobre la ventana: flechas mueven 1 px (Shift = 10);
  Ctrl+flechas estiran/encogen 1 px (Ctrl+Shift = 10).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QPushButton, QSlider,
    QToolButton, QVBoxLayout, QWidget,
)

from . import theme

SNAP_PX = 12          # distancia a la que los bordes se atraen
_SNAP_ON = [True]     # imán global (Settings del hub)


def set_snap_enabled(on: bool) -> None:
    _SNAP_ON[0] = bool(on)


def _mini_btn(text: str, tip: str, checkable: bool = False) -> QToolButton:
    btn = QToolButton()
    btn.setText(text)
    btn.setToolTip(tip)
    btn.setCheckable(checkable)
    btn.setAutoRaise(True)
    btn.setFixedSize(20, 16)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


class _FloatWindow(QWidget):
    """Ventana propia de un panel desacoplado."""

    def __init__(self, holder: "Detachable"):
        super().__init__(None, Qt.Window)
        self.holder = holder
        self.pinned = False
        self._snap_guard = False
        self.setObjectName("floatwin")
        self.setWindowTitle(f"BoxBox-F1 — {holder.title}")
        self.setFocusPolicy(Qt.StrongFocus)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(2)
        bar = QHBoxLayout()
        title = QLabel(holder.title)
        title.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-weight: bold;")
        title.setToolTip(
            "Precise placement: arrow keys move 1 px (Shift = 10);\n"
            "Ctrl+arrows resize 1 px (Ctrl+Shift = 10). Windows snap\n"
            "to each other and to screen edges when close.")
        bar.addWidget(title)
        bar.addStretch(1)
        self.shot_btn = _mini_btn(
            "📷", "Capture this panel: copies the image to the clipboard "
                  "and offers to save it as PNG")
        self.shot_btn.clicked.connect(
            lambda _=False: holder.capture_panel(ask_save=True))
        bar.addWidget(self.shot_btn)
        # restaurar los subpaneles internos ocultados/flotados (solo visible
        # en ventanas que los tienen, p.ej. tablas de Times/Gap o tarjetas
        # de Quali): sin esto un subpanel cerrado con ✕ era irrecuperable
        self.reset_btn = _mini_btn(
            "↺", "Reset inner panels: reopen and dock back this window's "
                 "sub-panels")
        self.reset_btn.clicked.connect(self._reset_inner)
        self.reset_btn.setVisible(False)
        bar.addWidget(self.reset_btn)
        # opacidad para overlays fijados sobre una transmisión
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(55, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.setFixedWidth(64)
        self.opacity_slider.setToolTip("Opacity (only while pinned)")
        self.opacity_slider.valueChanged.connect(
            lambda v: self.setWindowOpacity(v / 100.0) if self.pinned else None)
        self.opacity_slider.setVisible(False)
        bar.addWidget(self.opacity_slider)
        self.pin_btn = _mini_btn(
            "📌", "Pin: keep on top, frameless and immovable", checkable=True
        )
        self.pin_btn.toggled.connect(self._set_pinned)
        bar.addWidget(self.pin_btn)
        if not holder.window_only:
            dock_btn = _mini_btn("⇱", "Dock back into the main window")
            dock_btn.clicked.connect(holder.attach)
            bar.addWidget(dock_btn)
        lay.addLayout(bar)
        self._lay = lay

    def add_content(self, widget: QWidget) -> None:
        self._lay.addWidget(widget, stretch=1)
        self.reset_btn.setVisible(bool(widget.findChildren(Detachable)))

    def _reset_inner(self) -> None:
        for sub in self.holder.content.findChildren(Detachable):
            sub.attach()
            sub.set_panel_visible(True)

    def _set_pinned(self, on: bool) -> None:
        # sin marco no se puede mover ni redimensionar; el botón de des-fijar
        # sigue disponible en la barrita interna
        self.pinned = on
        geom = self.geometry()
        self.setWindowFlag(Qt.FramelessWindowHint, on)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.setStyleSheet(
            f"QWidget#floatwin {{ border: 1px solid {theme.ACCENT}; }}" if on else ""
        )
        self.opacity_slider.setVisible(on)
        self.setWindowOpacity(self.opacity_slider.value() / 100.0 if on else 1.0)
        self.show()
        self.setGeometry(geom)
        self.raise_()
        self.activateWindow()
        self.holder.stateChanged.emit()

    def closeEvent(self, event) -> None:
        # cerrar la ventana solo la oculta: el panel sigue "flotante" y se
        # reabre desde el menú Panels
        event.ignore()
        self.hide()
        self.holder.stateChanged.emit()

    # ------------------------------------------- imán y colocación precisa

    def _snap_edges(self) -> tuple[list, list]:
        """Bordes candidatos (xs, ys) de las otras ventanas de la app y de
        la pantalla. Para cada eje: posiciones a las que puede pegarse el
        borde izquierdo/derecho (o superior/inferior) propio."""
        g = self.frameGeometry()
        xs: list[tuple[int, int]] = []   # (x_objetivo_para_left, peso)
        ys: list[tuple[int, int]] = []
        for w in QApplication.topLevelWidgets():
            if w is self or not isinstance(w, _FloatWindow) \
                    or not w.isVisible():
                continue
            o = w.frameGeometry()
            v_overlap = (g.top() <= o.bottom() + SNAP_PX
                         and o.top() <= g.bottom() + SNAP_PX)
            h_overlap = (g.left() <= o.right() + SNAP_PX
                         and o.left() <= g.right() + SNAP_PX)
            if v_overlap:
                # adyacencia sin hueco y alineación de bordes
                xs += [(o.x() + o.width(), 0), (o.x() - g.width(), 0),
                       (o.x(), 1), (o.x() + o.width() - g.width(), 1)]
            if h_overlap:
                ys += [(o.y() + o.height(), 0), (o.y() - g.height(), 0),
                       (o.y(), 1), (o.y() + o.height() - g.height(), 1)]
        screen = self.screen()
        if screen is not None:
            a = screen.availableGeometry()
            xs += [(a.left(), 0), (a.right() + 1 - g.width(), 0)]
            ys += [(a.top(), 0), (a.bottom() + 1 - g.height(), 0)]
        return xs, ys

    def _apply_snap(self) -> None:
        if (self._snap_guard or not _SNAP_ON[0] or self.pinned
                or not self.isVisible()):
            return
        g = self.frameGeometry()
        xs, ys = self._snap_edges()
        best_dx = min((x - g.x() for x, _w in xs
                       if abs(x - g.x()) <= SNAP_PX),
                      key=abs, default=0)
        best_dy = min((y - g.y() for y, _w in ys
                       if abs(y - g.y()) <= SNAP_PX),
                      key=abs, default=0)
        if best_dx or best_dy:
            self._snap_guard = True
            self.move(self.pos().x() + best_dx, self.pos().y() + best_dy)
            self._snap_guard = False

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._apply_snap()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # al estirar, el borde derecho/inferior también se imanta (a la
        # izquierda de otra ventana, a su borde derecho, o a la pantalla)
        if (self._snap_guard or not _SNAP_ON[0] or self.pinned
                or not self.isVisible()):
            return
        g = self.frameGeometry()
        right, bottom = g.x() + g.width(), g.y() + g.height()
        rights: list[int] = []
        bottoms: list[int] = []
        for w in QApplication.topLevelWidgets():
            if w is self or not isinstance(w, _FloatWindow) \
                    or not w.isVisible():
                continue
            o = w.frameGeometry()
            if g.top() <= o.bottom() + SNAP_PX \
                    and o.top() <= g.bottom() + SNAP_PX:
                rights += [o.x(), o.x() + o.width()]
            if g.left() <= o.right() + SNAP_PX \
                    and o.left() <= g.right() + SNAP_PX:
                bottoms += [o.y(), o.y() + o.height()]
        screen = self.screen()
        if screen is not None:
            a = screen.availableGeometry()
            rights.append(a.right() + 1)
            bottoms.append(a.bottom() + 1)
        dw = min((r - right for r in rights if abs(r - right) <= SNAP_PX),
                 key=abs, default=0)
        dh = min((b - bottom for b in bottoms
                  if abs(b - bottom) <= SNAP_PX), key=abs, default=0)
        if dw or dh:
            self._snap_guard = True
            self.resize(self.width() + dw, self.height() + dh)
            self._snap_guard = False

    def keyPressEvent(self, event) -> None:
        step = 10 if event.modifiers() & Qt.ShiftModifier else 1
        dx = {Qt.Key_Left: -step, Qt.Key_Right: step}.get(event.key(), 0)
        dy = {Qt.Key_Up: -step, Qt.Key_Down: step}.get(event.key(), 0)
        if not dx and not dy:
            super().keyPressEvent(event)
            return
        if event.modifiers() & Qt.ControlModifier:
            self.resize(max(120, self.width() + dx),
                        max(80, self.height() + dy))
        else:
            self._snap_guard = True  # el ajuste fino no debe re-imantarse
            self.move(self.pos().x() + dx, self.pos().y() + dy)
            self._snap_guard = False
        event.accept()


class Detachable(QWidget):
    """Contenedor acoplado de un panel, con su barrita de control."""

    stateChanged = Signal()  # visibilidad / flotado / fijado cambió

    def __init__(self, panel_id: str, title: str, content: QWidget,
                 parent=None, keep_placeholder: bool = False,
                 closable: bool = True, window_only: bool = False):
        super().__init__(parent)
        self.panel_id = panel_id
        self.title = title
        self.content = content
        # ventana-pura: el panel vive SIEMPRE en su propia ventana; el
        # contenedor acoplado nunca se muestra y "visible" = ventana mostrada
        # (ocultar conserva la ventana para no perder geometría ni fijado)
        self.window_only = window_only
        self._win: _FloatWindow | None = None
        # paneles centrales: al flotar dejan un aviso en su lugar en vez de
        # colapsar el hueco (el centro del modo no queda vacío sin aviso)
        self._keep_placeholder = keep_placeholder
        self._placeholder: QWidget | None = None
        # oculto POR EL USUARIO (isHidden no sirve: el stack de modos oculta
        # las páginas no activas y daría falsos positivos)
        self._user_hidden = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(2, 0, 2, 0)
        label = QLabel(title)
        label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 8pt;")
        bar.addWidget(label)
        bar.addStretch(1)
        # lambda: clicked(bool) pasaría el checked como "geometry"
        self.float_btn = _mini_btn("⧉", "Detach into its own window")
        self.float_btn.clicked.connect(lambda _=False: self.detach())
        bar.addWidget(self.float_btn)
        if closable and not keep_placeholder:
            close_btn = _mini_btn(
                "✕", "Hide this sub-panel (↺ in the window bar brings it back)")
            close_btn.clicked.connect(self._hide_docked)
            bar.addWidget(close_btn)
        lay.addLayout(bar)
        self._lay = lay
        lay.addWidget(content, stretch=1)
        if window_only:
            self.hide()  # el contenedor acoplado jamás se muestra

    # ------------------------------------------------------------ estado

    @property
    def floating(self) -> bool:
        return self._win is not None

    @property
    def pinned(self) -> bool:
        return self._win is not None and self._win.pinned

    def is_panel_visible(self) -> bool:
        if self.window_only:
            return self._win is not None and self._win.isVisible()
        return self._win.isVisible() if self._win else not self._user_hidden

    def _hide_docked(self) -> None:
        self._user_hidden = True
        self.setVisible(False)
        self.stateChanged.emit()

    def set_panel_visible(self, on: bool) -> None:
        if self.window_only:
            if on:
                if self._win is None:
                    self.detach()
                else:
                    self._win.show()
                    self._win.raise_()
                    self._win.activateWindow()
            elif self._win is not None:
                self._win.hide()
            self.stateChanged.emit()
            return
        self._user_hidden = not on
        if self._win is not None:
            self._win.setVisible(on)
        else:
            self.setVisible(on)
        self.stateChanged.emit()

    def apply_visible(self, on: bool) -> None:
        """Aplicación programática (cambio de modo / restauración): fija el
        estado sin emitir stateChanged ni tocar ventanas flotantes."""
        if self.window_only:
            if on:
                if self._win is None:
                    self.detach()
                else:
                    self._win.show()
            elif self._win is not None:
                self._win.hide()
            return
        if self._win is not None:
            return
        self._user_hidden = not on
        self.setVisible(on)

    # ---------------------------------------------------- flotar / acoplar

    def detach(self, geometry: QRect | None = None, pinned: bool = False,
               show: bool = True) -> None:
        if self._win is not None:
            return
        win = _FloatWindow(self)
        self._win = win
        origin = self.mapToGlobal(self.rect().topLeft())
        size = self.content.size()
        self.content.setParent(None)
        win.add_content(self.content)
        self.content.show()
        if self._keep_placeholder:
            self._show_placeholder()
        else:
            self.hide()
        if isinstance(geometry, QRect):
            win.setGeometry(geometry)
        else:
            win.resize(max(size.width(), 320), max(size.height(), 240))
            win.move(origin)
        if show:
            win.show()
            win.raise_()          # al frente: si no, nace detrás de la ventana
            win.activateWindow()  # principal y parece que no se abrió
        if pinned:
            win.pin_btn.setChecked(True)
        self.stateChanged.emit()

    def attach(self) -> None:
        if self.window_only:  # sin acople: "cerrar" es ocultar la ventana
            self.set_panel_visible(False)
            return
        win, self._win = self._win, None
        if win is None:
            return
        win.hide()
        if self._placeholder is not None:
            self._lay.removeWidget(self._placeholder)
            self._placeholder.hide()
        self.content.setParent(None)
        self._lay.addWidget(self.content, stretch=1)
        self.content.show()
        win.deleteLater()
        self.show()
        self.stateChanged.emit()

    def _show_placeholder(self) -> None:
        if self._placeholder is None:
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.addStretch(1)
            note = QLabel(f"{self.title} is floating in its own window")
            note.setAlignment(Qt.AlignCenter)
            note.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            lay.addWidget(note)
            btn = QPushButton("⇱ Dock it back")
            btn.clicked.connect(self.attach)
            lay.addWidget(btn, alignment=Qt.AlignCenter)
            lay.addStretch(1)
            self._placeholder = box
        self._lay.addWidget(self._placeholder, stretch=1)
        self._placeholder.show()

    def capture_panel(self, ask_save: bool = True):
        """Captura el contenido del panel: SIEMPRE al portapapeles y, si
        ask_save, ofrece guardarla como PNG (cancelar deja solo la copia)."""
        pix = self.content.grab()
        QApplication.clipboard().setPixmap(pix)
        if ask_save:
            safe = re.sub(r"[^\w\- ]", "", self.title).strip() or "panel"
            stamp = time.strftime("%Y%m%d-%H%M%S")
            suggested = str(Path.home() / f"BoxBox-F1 {safe} {stamp}.png")
            path, _flt = QFileDialog.getSaveFileName(
                self._win or self, "Save panel image (already copied to "
                "the clipboard)", suggested, "PNG image (*.png)")
            if path:
                pix.save(path, "PNG")
        return pix

    def close_float(self) -> None:
        """Cierra la ventana flotante al salir de la app: el contenido vuelve
        al contenedor (destruir la ventana lo destruiría con ella)."""
        win, self._win = self._win, None
        if win is None:
            return
        win.hide()
        self.content.setParent(None)
        self._lay.addWidget(self.content, stretch=1)
        win.deleteLater()

    # ------------------------------------------------------- persistencia

    def save_state(self) -> dict:
        state = {"floating": self.floating or self.window_only,
                 "visible": self.is_panel_visible()}
        if self._win is not None:
            g = self._win.geometry()
            state["geom"] = [g.x(), g.y(), g.width(), g.height()]
            state["pinned"] = self._win.pinned
            state["opacity"] = self._win.opacity_slider.value()
        return state

    def restore_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        if state.get("floating") or self.window_only:
            geom = state.get("geom")
            rect = QRect(*geom) if isinstance(geom, list) and len(geom) == 4 else None
            visible = bool(state.get("visible", True))
            opacity = state.get("opacity")
            if self.window_only and self._win is not None:
                # re-aplicar sobre una ventana ya creada (cambio de perfil)
                if rect is not None:
                    self._win.setGeometry(rect)
                if isinstance(opacity, (int, float)):
                    self._win.opacity_slider.setValue(int(opacity))
                if self._win.pin_btn.isChecked() != bool(state.get("pinned")):
                    self._win.pin_btn.setChecked(bool(state.get("pinned")))
                self._win.setVisible(visible)
                return
            self.detach(rect, bool(state.get("pinned")), show=visible)
            if self._win is not None and isinstance(opacity, (int, float)):
                self._win.opacity_slider.setValue(int(opacity))
                if self._win.pinned:
                    self._win.setWindowOpacity(int(opacity) / 100.0)
        elif not state.get("visible", True):
            self._user_hidden = True
            self.setVisible(False)
