"""Paneles desacoplables: cualquier widget envuelto en `Detachable` gana una
barrita de título con botones para desacoplarlo a una ventana propia (con
marco nativo: se mueve y redimensiona libremente), fijarlo (sin marco,
siempre encima e inamovible), ocultarlo y volver a acoplarlo donde estaba.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSlider, QToolButton, QVBoxLayout,
    QWidget,
)

from . import theme


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
        self.setObjectName("floatwin")
        self.setWindowTitle(f"BoxBox-F1 — {holder.title}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(2)
        bar = QHBoxLayout()
        title = QLabel(holder.title)
        title.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-weight: bold;")
        bar.addWidget(title)
        bar.addStretch(1)
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
