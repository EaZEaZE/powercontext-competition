# 规格 v2：预留过期（TTL）变更需求

状态：待实现（2026-11-11 提出）
对应变更单：CR-2026-1111-TTL
基线规格：`specs/spec-v1.md`

## 1. 背景

v1 中预留一旦创建就永久占用库存。用户下单后放弃支付时，没有任何机制把库存还回去，
导致库存长期被僵尸预留锁死。运营侧统计：约 12% 的预留最终不会被 commit 也不会被显式 release。

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

## 3. 必须保持不变的原有行为（回归约束）

以下为硬约束，任何一条被破坏都视为变更失败：

- `reserve(sku, quantity)` 两参数调用方式必须继续可用，且不产生过期
- `add_stock` / `get_stock` / `get_available` 的签名与返回类型不变
- `release` / `commit` 对未知 ID、终态 ID 仍返回 `False`，不得改为抛异常
- `commit` 仍真实扣减 `get_stock`，`release` 仍不扣减
- `InventoryError` 仍是唯一业务异常类型，且仍是 `ValueError` 子类
- `reservation_id` 仍以 `rsv-` 开头且唯一
- `list_reservations(None)` 仍返回全部预留（含过期），状态过滤语义不变

## 4. 验收标准

1. 新增用例覆盖：TTL 内可 commit、TTL 过期后 available 恢复、过期后 release/commit 返回 False、
   `ttl_seconds<=0` 抛错、`ttl_seconds=None` 不退化为会过期。
2. v1 的 12 个既有用例全部通过，不得修改既有用例的断言。
3. 不引入第三方依赖，不引入后台线程。
