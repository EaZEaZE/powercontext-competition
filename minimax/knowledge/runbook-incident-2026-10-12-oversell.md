# 事故复盘：2026-10-12 秒杀场景超卖

## 现象

大促开抢后 40 秒，SKU-7731 出现 37 单超卖。日志显示同一批预留全部返回成功，
但可用量在某一刻被算成了负数。

## 根因

1. `get_available` 每次遍历全部预留求和，属于 O(n) 扫描；
2. 下单服务在拿到 `reserve` 返回值之前就并发发起下一笔，扫到的是旧集合；
3. 没有终态过滤时，`released` 的预留被重复计入占用，导致可用量偏低——修复过程中又反向改错，
   一度把 `committed` 也算进占用，出现可用量虚高。

## 修复

- `get_available` 只统计 `status == "held"` 的预留；
- 终态（`released` / `committed`）不再占用额度；
- 新增回归用例 `test_commit_released_reservation_returns_false` 与 `test_release_twice_returns_false`。

## 给后续变更的约束

任何新增状态（例如 `expired`）都必须显式纳入「是否占用额度」的判断，
不得用 `status != "released"` 这类反向条件，否则新状态会被默认当成占用。
