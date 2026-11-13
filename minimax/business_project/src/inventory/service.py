"""库存预留服务。

对外契约见 specs/spec-v1.md，修改需求见 specs/spec-v2.md。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional


class InventoryError(ValueError):
    """业务校验失败时抛出。"""


@dataclass
class Reservation:
    reservation_id: str
    sku: str
    quantity: int
    status: str = "held"  # held | committed | released


class InventoryService:
    def __init__(self) -> None:
        self._stock: Dict[str, int] = {}
        self._reservations: Dict[str, Reservation] = {}

    # ---------- 库存 ----------
    def add_stock(self, sku: str, quantity: int) -> int:
        self._check_quantity(quantity)
        if not isinstance(sku, str) or not sku:
            raise InventoryError("sku 必须是非空字符串")
        self._stock[sku] = self._stock.get(sku, 0) + quantity
        return self._stock[sku]

    def get_stock(self, sku: str) -> int:
        return self._stock.get(sku, 0)

    def get_available(self, sku: str) -> int:
        held = sum(r.quantity for r in self._reservations.values()
                   if r.sku == sku and r.status == "held")
        return self.get_stock(sku) - held

    # ---------- 预留 ----------
    def reserve(self, sku: str, quantity: int) -> Reservation:
        self._check_quantity(quantity)
        if sku not in self._stock:
            raise InventoryError("未知 sku: %s" % sku)
        if quantity > self.get_available(sku):
            raise InventoryError(
                "可用库存不足: sku=%s 需要=%d 可用=%d"
                % (sku, quantity, self.get_available(sku))
            )
        reservation = Reservation(
            reservation_id="rsv-" + uuid.uuid4().hex[:12],
            sku=sku,
            quantity=quantity,
        )
        self._reservations[reservation.reservation_id] = reservation
        return reservation

    def release(self, reservation_id: str) -> bool:
        reservation = self._reservations.get(reservation_id)
        if reservation is None or reservation.status != "held":
            return False
        reservation.status = "released"
        return True

    def commit(self, reservation_id: str) -> bool:
        reservation = self._reservations.get(reservation_id)
        if reservation is None or reservation.status != "held":
            return False
        if reservation.quantity > self.get_stock(reservation.sku):
            raise InventoryError("库存不足，无法出库: %s" % reservation.sku)
        self._stock[reservation.sku] -= reservation.quantity
        reservation.status = "committed"
        return True

    def get_reservation(self, reservation_id: str) -> Optional[Reservation]:
        return self._reservations.get(reservation_id)

    def list_reservations(self, status: Optional[str] = None) -> List[Reservation]:
        items = list(self._reservations.values())
        if status is None:
            return items
        return [r for r in items if r.status == status]

    # ---------- 内部 ----------
    @staticmethod
    def _check_quantity(quantity: int) -> None:
        if not isinstance(quantity, int) or isinstance(quantity, bool):
            raise InventoryError("quantity 必须是整数")
        if quantity <= 0:
            raise InventoryError("quantity 必须大于 0")
