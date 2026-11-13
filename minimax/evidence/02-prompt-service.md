## 检索到的工程资料（唯一可信依据）

# 检索到的工程资料（来自 seekdb 向量检索）

检索语句：按 specs/spec-v2.md 的 CR-2026-1111-TTL，为库存预留增加可选的 ttl_seconds 过期能力。要求保持 spec-v1 的全部既有行为不变。

## [1] specs/spec-v2.md › 规格 v2：预留过期（TTL）变更需求（相似度 0.327）

# 规格 v2：预留过期（TTL）变更需求

# 规格 v2：预留过期（TTL）变更需求

状态：待实现（2026-11-11 提出）
对应变更单：CR-2026-1111-TTL
基线规格：`specs/spec-v1.md`

## [2] specs/spec-v2.md › 2. 变更内容（相似度 0.303）

# 2. 变更内容

## 2. 变更内容

`reserve` 增加一个**可选**参数 `ttl_seconds`：

```python
def reserve(self, sku: str, quantity: int, ttl_seconds: int | None = None) -> Reservation
```

行为：

1. `ttl_seconds is None` 时，行为必须与 v1 **完全一致**（永久持有，不会自动过期）。
2. `ttl_seconds` 为正整数时，该预留在创建时刻 + `ttl_seconds` 秒后过期。
3. 过期后该预留状态变为 `expired`，其占用的数量重新计入 `get_available`。
4. `ttl_seconds <= 0` 或非整数时抛 `InventoryError`。
5. 过期判断必须是**惰性**的：在 `get_available`、`get_reservation`、`list_reservations`、
   `release`、`commit` 被调用时顺带结算，不引入后台线程或定时器。
6. 对已过期预留调用 `release` 或 `commit` 返回 `False`（与 v1 中已结束预留的语义一致）。
7. `Reservation` 需要新增 `expires_at: float | None` 字段（`None` 表示不过期）。
8. `status` 枚举从三值扩展为四值，新增 `expired`。

## [3] specs/spec-v2.md › 3. 必须保持不变的原有行为（回归约束）（相似度 0.284）

# 3. 必须保持不变的原有行为（回归约束）

## 3. 必须保持不变的原有行为（回归约束）

以下为硬约束，任何一条被破坏都视为变更失败：

- `reserve(sku, quantity)` 两参数调用方式必须继续可用，且不产生过期
- `add_stock` / `get_stock` / `get_available` 的签名与返回类型不变
- `release` / `commit` 对未知 ID、终态 ID 仍返回 `False`，不得改为抛异常
- `commit` 仍真实扣减 `get_stock`，`release` 仍不扣减
- `InventoryError` 仍是唯一业务异常类型，且仍是 `ValueError` 子类
- `reservation_id` 仍以 `rsv-` 开头且唯一
- `list_reservations(None)` 仍返回全部预留（含过期），状态过滤语义不变

## [4] knowledge/conventions-python.md › 提交要求（相似度 0.277）

# 提交要求

## 提交要求

- 提交信息格式：`<scope>: <动词> <对象>`，例如 `inventory: add reservation ttl`
- 变更必须附带「改了什么」与「哪些行为必须保持不变」两段说明
- 涉及状态机变更时，必须同步更新规格文档中的状态机小节

## [5] specs/spec-v1.md › 规格 v1：库存预留服务对外契约（相似度 0.277）

# 规格 v1：库存预留服务对外契约

# 规格 v1：库存预留服务对外契约

状态：已发布（2026-10-01）
模块：`business_project/src/inventory/service.py`

## [6] knowledge/adr-0001-reservation-lifecycle.md › 后果（相似度 0.264）

# 后果

## 后果

- 必须接受「僵尸预留」风险：用户放弃支付后额度长期占用。这正是 CR-2026-1111-TTL 要解决的问题。
- 任何新增的过期逻辑都只能影响 `held` 状态，不能触碰 `committed` / `released`。


## 当前实现（src/inventory/service.py）

```python
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

```

## 本次要求

按 specs/spec-v2.md 的 CR-2026-1111-TTL，为库存预留增加可选的 ttl_seconds 过期能力。要求保持 spec-v1 的全部既有行为不变。

## 输出要求

输出 `src/inventory/service.py` 的完整新版本，放在一个 ```python 代码块中。

不要输出其他文件，不要输出解释。
