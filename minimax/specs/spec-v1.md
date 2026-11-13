# 规格 v1：库存预留服务对外契约

状态：已发布（2026-10-01）
模块：`business_project/src/inventory/service.py`

## 1. 目标

为下单流程提供库存扣减能力：先预留（hold），确认后出库（commit），取消则释放（release）。
预留期间库存被占用但未真正扣除，避免超卖。

## 2. 数据结构

`Reservation` 为不可变语义的取值对象，字段固定：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `reservation_id` | str | 形如 `rsv-xxxxxxxxxxxx`，全局唯一 |
| `sku` | str | 商品编码 |
| `quantity` | int | 预留数量，恒大于 0 |
| `status` | str | 枚举：`held` / `committed` / `released`，初始为 `held` |

## 3. 对外方法

| 方法 | 签名 | 返回 | 约定 |
| --- | --- | --- | --- |
| `add_stock` | `(sku: str, quantity: int)` | `int` 入库后的总库存 | `quantity<=0` 抛 `InventoryError`；`sku` 为空抛 `InventoryError` |
| `get_stock` | `(sku: str)` | `int` 总库存，未登记返回 0 | 不抛异常 |
| `get_available` | `(sku: str)` | `int` 总库存 − `held` 数量 | 不抛异常 |
| `reserve` | `(sku: str, quantity: int)` | `Reservation` | 未知 sku 抛错；超过可用量抛错；成功返回 `status=held` |
| `release` | `(reservation_id: str)` | `bool` | 仅 `held` 状态可释放；未知或已结束返回 `False`，不抛异常 |
| `commit` | `(reservation_id: str)` | `bool` | 仅 `held` 可出库；未知或已结束返回 `False`；真实扣减 `get_stock` |
| `get_reservation` | `(reservation_id: str)` | `Reservation \| None` | 未知返回 `None` |
| `list_reservations` | `(status: str \| None = None)` | `list[Reservation]` | `status=None` 返回全部 |

## 4. 状态机

```
held --release--> released
held --commit---> committed
```

状态只允许单向迁移；`released` 与 `committed` 均为终态，不可再次迁移。
重复调用 `release` / `commit` 必须返回 `False` 而不是抛异常。

## 5. 错误处理

所有业务校验失败统一抛 `InventoryError`（`ValueError` 的子类），调用方据此返回 400。
不得抛 `KeyError`、`TypeError`、裸 `Exception`。

## 6. 并发与幂等

当前实现为单进程内存态，不做并发保护；同一 `reservation_id` 的重复操作必须幂等返回 `False`。

## 7. 明确不做的事

- 不做持久化，进程重启后数据清空
- 不做跨 sku 的批量预留
- 不做价格、促销、税费计算（见 pricing 模块，二者互不依赖）
