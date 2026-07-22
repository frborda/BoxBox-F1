"""Botones 👥 de autos por ventana.

Dos variantes sobre el mismo popup (lista de autos con checkbox):

- `DriverFilterButton` (mapa, rueda, torre): filtro de VISIBLES, todos por
  defecto; guarda el conjunto de ocultos por ventana en la configuración.
- `DriverSelectButton` (gráficos de comparación): selección local que nace
  del panel Drivers — cada cambio en ese panel la vuelve a pisar — y se
  retoca por ventana sin afectar a las demás. No se persiste.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QListWidget, QListWidgetItem, QMenu, QToolButton, QVBoxLayout,
    QWidget, QWidgetAction,
)

from .. import config
from ..hub import DataHub


class _DriverListButton(QToolButton):
    """Base: botón 👥 que despliega la lista de autos con un checkbox por
    auto. Las subclases definen qué significa el tilde."""

    changed = Signal()

    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setPopupMode(QToolButton.InstantPopup)
        self.setStyleSheet("QToolButton::menu-indicator { image: none; }")

        menu = QMenu(self)
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(2)
        self.all_check = QCheckBox("All cars")
        self.all_check.clicked.connect(self._toggle_all)
        lay.addWidget(self.all_check)
        self.list = QListWidget()
        self.list.setMinimumSize(160, 240)
        self.list.itemChanged.connect(self._item_changed)
        lay.addWidget(self.list)
        action = QWidgetAction(menu)
        action.setDefaultWidget(box)
        menu.addAction(action)
        self.setMenu(menu)

        hub.driversChanged.connect(self._rebuild)
        self._rebuild()

    # ganchos de las subclases -------------------------------------------

    def _is_checked(self, drv: str) -> bool:
        raise NotImplementedError

    def _apply_item(self, drv: str, on: bool) -> None:
        raise NotImplementedError

    def _apply_all(self, on: bool) -> None:
        raise NotImplementedError

    def _label(self, shown: int, total: int) -> str:
        raise NotImplementedError

    # mecánica común ------------------------------------------------------

    def _numbers(self) -> list[str]:
        return [str(self.list.item(i).data(Qt.UserRole))
                for i in range(self.list.count())]

    def _rebuild(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        drivers = sorted(self.hub.drivers.values(), key=lambda d: d.label.upper())
        for info in drivers:
            item = QListWidgetItem(info.label)
            item.setData(Qt.UserRole, info.number)
            pix = QPixmap(12, 12)
            pix.fill(QColor(info.color))
            item.setIcon(QIcon(pix))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if self._is_checked(info.number)
                               else Qt.Unchecked)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._sync_button()

    def _item_changed(self, item: QListWidgetItem) -> None:
        self._apply_item(str(item.data(Qt.UserRole)),
                         item.checkState() == Qt.Checked)
        self._sync_button()
        self.changed.emit()

    def _toggle_all(self, on: bool) -> None:
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.list.blockSignals(False)
        self._apply_all(on)
        self._sync_button()
        self.changed.emit()

    def _sync_button(self) -> None:
        total = self.list.count()
        shown = sum(1 for i in range(total)
                    if self.list.item(i).checkState() == Qt.Checked)
        self.setText(self._label(shown, total))
        self.all_check.blockSignals(True)
        self.all_check.setChecked(total > 0 and shown == total)
        self.all_check.blockSignals(False)
        self.adjustSize()  # fuera de un layout (overlay del mapa) no crece solo


class DriverFilterButton(_DriverListButton):
    """Filtro de autos VISIBLES de una ventana (mapa, rueda, torre): todos
    por defecto; guarda los ocultos en config bajo ui.<cfg_key>."""

    def __init__(self, hub: DataHub, cfg: dict | None = None,
                 cfg_key: str = "", parent=None):
        self.cfg = cfg
        self.cfg_key = cfg_key
        stored = (cfg or {}).get("ui", {}).get(cfg_key) if cfg_key else None
        self.hidden: set[str] = ({str(d) for d in stored}
                                 if isinstance(stored, list) else set())
        super().__init__(hub, parent)
        self.setToolTip("Cars shown in this window (all by default) — "
                        "independent of the Drivers comparison panel")

    def is_visible(self, drv: str) -> bool:
        return drv not in self.hidden

    def filter(self, drivers) -> list[str]:
        return [d for d in drivers if d not in self.hidden]

    def _is_checked(self, drv: str) -> bool:
        return drv not in self.hidden

    def _apply_item(self, drv: str, on: bool) -> None:
        if on:
            self.hidden.discard(drv)
        else:
            self.hidden.add(drv)
        self._save()

    def _apply_all(self, on: bool) -> None:
        self.hidden = set() if on else set(self._numbers())
        self._save()

    def _label(self, shown: int, total: int) -> str:
        return "👥" if shown == total else f"👥 {shown}/{total}"

    def _save(self) -> None:
        if self.cfg and self.cfg_key:
            self.cfg.setdefault("ui", {})[self.cfg_key] = sorted(self.hidden)
            config.save_config(self.cfg)


class DriverSelectButton(_DriverListButton):
    """Selección local de autos de un gráfico de comparación: nace del
    panel Drivers (`set_selection`, que la pisa en cada cambio de ese
    panel) y se retoca acá sin tocar a las otras ventanas."""

    def __init__(self, hub: DataHub, parent=None):
        self.selected: set[str] = set()
        super().__init__(hub, parent)
        self.setToolTip("Cars charted in this window — starts as the "
                        "Drivers panel selection (any change there resets "
                        "this window)")

    def selection(self) -> list[str]:
        return [d for d in self._numbers() if d in self.selected]

    def set_selection(self, drivers) -> None:
        """Pisa la selección local (panel Drivers); no emite `changed`."""
        self.selected = {str(d) for d in drivers}
        self._rebuild()

    def _is_checked(self, drv: str) -> bool:
        return drv in self.selected

    def _apply_item(self, drv: str, on: bool) -> None:
        if on:
            self.selected.add(drv)
        else:
            self.selected.discard(drv)

    def _apply_all(self, on: bool) -> None:
        self.selected = set(self._numbers()) if on else set()

    def _label(self, shown: int, total: int) -> str:
        return f"👥 {shown}/{total}"
