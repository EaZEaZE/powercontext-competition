# MiniMax 赛题 · 用 MiniMax 驱动 seekdb 检索的工程化改码流水线

## 一句话方案

把工程资料（规格、ADR、约定、runbook）切块写入 **seekdb** 向量库；改码时先从 seekdb 检索出可信上下文，
再由 **MiniMax** 完成「改实现 → 补功能测试 → 失败自修复」，最后用 pytest 基线/回归双跑和 diff 作为证据。

## 赛题要求对照

| 要求 | 交付内容 |
|---|---|
| 源码仓库 | 本目录（`minimax/`），流水线 + 业务工程 + 知识库 |
| 运行说明 | 见下方「环境准备」「运行步骤」 |
| 验证样例 | 见下方「验证样例」 |
| 代码差异 | [`evidence/04-code-diff.patch`](./evidence/04-code-diff.patch) |
| 测试结果 | [`evidence/05-pytest-baseline.txt`](./evidence/05-pytest-baseline.txt)、[`evidence/06-pytest-after.txt`](./evidence/06-pytest-after.txt)、[`evidence/07-pytest-split.txt`](./evidence/07-pytest-split.txt) |
| 技术使用记录 | [`evidence/minimax-calls.jsonl`](./evidence/minimax-calls.jsonl)（每次调用的请求/响应/耗时/usage 全量落盘） |

## 架构

```
知识库 knowledge/ + specs/ + business_project/
        │  切块（按标题，≤700 字符）
        ▼
   ingest.py ──► seekdb collection "eng_docs"（HNSW, cosine, 512d）
        │
        ▼  retrieve.py（top-k=6）
   检索上下文 ──► MiniMax（改 service.py）──► MiniMax（补 test_ttl.py）
                                                        │
                                                        ▼
                                              pytest 基线 → 应用变更 → pytest 全量
                                                        │
                                            失败则把 pytest 输出回喂 MiniMax，最多 3 轮
                                                        │
                                                        ▼
                                     evidence/：context、prompt、response、diff、pytest、summary
```

## 目录

```
minimax/
├── business_project/          # 被改造的库存预留工程（v1 基线）
│   ├── src/inventory/service.py
│   └── tests/test_service.py  # 12 个既有用例，禁止修改断言
├── knowledge/                 # 可信工程资料
│   ├── adr-0001-reservation-lifecycle.md
│   ├── adr-0002-error-handling.md
│   ├── conventions-python.md
│   ├── module-pricing.md
│   ├── runbook-incident-2026-10-12-oversell.md
│   └── testing-policy.md
├── specs/
│   ├── spec-v1.md             # 基线规格
│   └── spec-v2.md             # 变更单 CR-2026-1111-TTL
├── tools/
│   ├── embed.py               # 本地确定性 embedding（字符 n-gram 哈希，512 维）
│   ├── ingest.py              # 切块 + 写入 seekdb
│   ├── retrieve.py            # 向量检索 + 构造上下文
│   ├── minimax_client.py      # MiniMax OpenAI 兼容 / 原生接口双通道
│   └── pipeline.py            # 五步编排
└── evidence/                  # 全部运行证据
```

## 环境准备

```bash
python 3.12
pip install pyseekdb pytest
export MINIMAX_API_KEY=...                 # 必填
export MINIMAX_BASE_URL=https://api.minimax.chat/v1   # 可选
export MINIMAX_MODEL=MiniMax-M2                       # 可选
export SEEKDB_PATH=/root/seekdb-data                  # seekdb 数据目录
export WORKSPACE=/root/run/workspace                  # 临时工作区
```

> embedding 使用本地哈希 n-gram（无外网模型下载依赖），向量维度 512，seekdb 侧 HNSW + cosine。

## 运行步骤

```bash
cd minimax

# 1. 建索引（首次或资料变更后执行）
python tools/ingest.py --root . --reset
# {"collection": "eng_docs", "chunks": N, "total": N}

# 2. 确认检索可用
python tools/retrieve.py "TTL 过期后库存如何恢复" -k 3

# 3. 跑完整流水线（改实现 → 补测试 → 回归 + 功能双跑 → 出证据）
python tools/pipeline.py --ingest
# 末尾打印 {"after_exit": "exit=0", "repair_rounds": 0, "regression_ok": true, "feature_ok": true}
```

产物全部写入 `evidence/`：

| 文件 | 内容 |
|---|---|
| `01-context.md` | seekdb 检索出的 top-6 上下文 |
| `02-prompt-service.md` / `02-prompt-tests.md` | 发给 MiniMax 的完整提示词 |
| `03-response-service.md` / `03-response-tests.md` | MiniMax 原始返回 |
| `04-code-diff.patch` | 相对 v1 基线的 unified diff |
| `05-pytest-baseline.txt` | 变更前全量测试 |
| `06-pytest-after.txt` | 变更后全量测试 |
| `07-pytest-split.txt` | 回归用例 / 新增功能用例分开统计 |
| `08-summary.json` | 结论汇总 |
| `minimax-calls.jsonl` | 每次 MiniMax 调用的 URL、模型、耗时、usage、请求与响应 |

## 验证样例

变更单 CR-2026-1111-TTL：为 `reserve` 增加可选 `ttl_seconds`，过期后库存自动归还，且 v1 行为完全不变。

| # | 验证点 | 期望 |
|---|---|---|
| 1 | TTL 内 commit | `commit` 返回 True，`get_stock` 扣减 |
| 2 | TTL 过期后 `get_available` | 占用数量恢复 |
| 3 | 过期后 `release` / `commit` | 均返回 False |
| 4 | `ttl_seconds <= 0` | 抛 `InventoryError` |
| 5 | `ttl_seconds=None` | 永不过期，等价 v1 |
| 6 | v1 既有 12 用例 | 全部通过，断言未改 |
| 7 | 依赖约束 | 无第三方依赖、无后台线程（惰性结算） |

实测结果：

```
回归测试 tests/test_service.py   12 passed
功能测试 tests/test_ttl.py         7 passed
after_exit=exit=0  repair_rounds=0
```

## 技术使用记录

`evidence/minimax-calls.jsonl` 每行一次调用，包含：时间戳、接口 URL、模型名、耗时、`usage`、
完整 `request`（system + user prompt）与完整 `response`。可直接用 jq 复核：

```bash
jq -c '{ts, model, elapsed_s, usage}' evidence/minimax-calls.jsonl
```
