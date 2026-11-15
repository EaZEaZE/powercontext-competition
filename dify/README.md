# Dify 赛题 · OceanBase SQL Helper

## 方案与验证结论

在 Dify 中用 **Workflow** 编排「自然语言需求 → 规则生成 SQL → HTTP 桥接 → 嵌入式 seekdb 实际执行 → 结果输出」。主流程不依赖外部模型密钥，DSL 无任何插件依赖（`dependencies: []`）。

上一轮评测反馈「DSL 配置存在阻断主要流程的错误」。排查定位到 DSL 版本：交付文件为 `0.1.5`，而 Dify 1.10.1 的 `CURRENT_DSL_VERSION` 为 `0.5.0`，导入必然返回 `completed-with-warnings`（[app_dsl_service.py](https://github.com/langgenius/dify/blob/1.10.1/api/services/app_dsl_service.py) 的 `_check_version_compatibility`）。现在 DSL 已按 0.5.0 重写并在全新实例中重新导入验证。

| 验证项 | 真实结果 | 证据 |
|---|---|---|
| 导入 DSL | HTTP 200，`status=completed`，`imported_dsl_version=0.5.0`，`error=""`，**无告警** | [`evidence/01-import.json`](./evidence/01-import.json) |
| 创建 128 维向量表 | 全部节点成功，seekdb 返回 `ok=true` | [`evidence/02-create-table.sse`](./evidence/02-create-table.sse) |
| 创建 HNSW/L2 索引 | `CREATE VECTOR INDEX idx_items_v ...` 实际执行成功 | [`evidence/04-create-index.sse`](./evidence/04-create-index.sse) |
| 统计记录数 | 实际返回 `rows=[["3"]]` | [`evidence/05-count.sse`](./evidence/05-count.sse) |
| 相似度检索 | 实际返回 ID `1、3、2`，距离 `0.0、0.14142137441313676、1.4142135623730951` | [`evidence/06-vector-search.sse`](./evidence/06-vector-search.sse) |
| 范围查询 SQL | 生成并执行 `WHERE id > 100 AND id < 1000 LIMIT 100;`（本环境无该范围数据） | [`evidence/07-range.sse`](./evidence/07-range.sse) |
| 桥接不可用 | Workflow 仍完成（`partial-succeeded`），输出 SQL 与降级说明，不中断 | [`evidence/08-bridge-down.sse`](./evidence/08-bridge-down.sse) |

验证方式：全新 Ubuntu 实例上从源码启动 Dify 1.10.1 的 API、Sandbox、PostgreSQL、Redis，通过 Console API 导入并运行；所有 SSE 文件为接口原始返回，`00-summary.json` 汇总三次导入与五次运行结果。

## 文件结构

```text
dify/
├── .env                         # 已验证源码实例的启动参数参考
├── deploy.sh                    # 启动已配置的 Dify 源码服务与 seekdb 桥接
├── dsl/
│   └── oceanbase-sql-helper.yml # 完整 Dify Workflow DSL（DSL 0.5.0）
├── bridge/
│   ├── seekdb_bridge.py         # 嵌入式 seekdb SQL 桥接
│   └── requirements.txt
└── evidence/                    # 导入响应、五次运行 SSE、种子向量
```

## DSL 结构与用途

应用模式：**Workflow**（`mode: workflow`），DSL `version: 0.5.0`，`dependencies: []`。

| 节点 | 类型 | 作用 |
|---|---|---|
| 开始 | start | 接收「需求描述 / 表名 / 向量维度 / 返回条数」四个可选入参 |
| 生成 SQL | code | 生成建表、向量索引、相似度检索、统计或范围查询 SQL |
| seekdb 执行 | http-request | POST `{{SEEKDB_BRIDGE_URL}}/sql`，超时 10s/30s/30s，失败走默认值 |
| 结果输出 | end | 输出 `sql`、`kind`、`seekdb_result` |

相似度检索使用与 `dim` 相同维度的字面量查询向量（默认 `[1,0,...]` 共 128 维），不引用未绑定变量。默认 `dim=128`、`topk=10`、`table=items`。

`seekdb_result` 中的 `ok=true` 与 `mode=embedded` 才能证明 seekdb 实际执行；桥接不可用时返回 `ok=false` 的明确说明，Workflow 不会中断。

## 启动已配置的源码实例

前置条件：已准备好 Dify 1.10.1、Dify Sandbox、PostgreSQL、Redis、Python 3.12 + uv、Node 22 + pnpm 及对应配置。`deploy.sh` 负责依赖补齐、数据库迁移和服务启动，不负责安装这些系统软件。

1. 按实际路径编辑 [`.env`](./.env)（`DIFY_SRC`、`DIFY_SANDBOX_DIR`、`SEEKDB_DIR`、`UV_BIN`、`PNPM_BIN`）。
2. Dify API 的完整配置位于 `$DIFY_SRC/api/.env`，Web 的 API 地址位于 `$DIFY_SRC/web/.env.local`；根目录 `.env` 不替代它们。
3. Sandbox 的 `python_lib_path` 必须包含 Python 标准库与系统包路径，否则代码节点可能报缺少 `json`。
4. 启动：`bash deploy.sh`，然后检查 `curl http://127.0.0.1:18080/health`。

正常桥接响应：

```json
{"ok":true,"mode":"embedded","seekdb_dir":"/root/seekdb-data","database":"test"}
```

也可只启动桥接：

```bash
cd bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
SEEKDB_DIR=/root/seekdb-data SEEKDB_DATABASE=test .venv/bin/python seekdb_bridge.py
```

## 导入与运行顺序

1. 打开 `http://<host>:3000/install` 初始化管理员（已初始化则直接登录）。
2. 「工作室 → 导入 DSL 文件」，选择 `dsl/oceanbase-sql-helper.yml`，确认状态为 `completed` 且无告警。
3. 确认环境变量 `SEEKDB_BRIDGE_URL=http://127.0.0.1:18080`，桥接在其他机器时改成实际地址并放行端口。
4. 发布应用，在 Workflow 页面按 **Alt+R** 打开 Test Run，填写「需求描述」并运行。
5. 在运行详情中检查 `seekdb_result` 是否为 `ok=true` 及真实 `rows`。

## 验证样例

| # | 需求描述 | 期望 `kind` | 已验证结果 |
|---|---|---|---|
| 1 | `帮我建个128维向量表` | `create_table` | 建表成功，`ok=true` |
| 2 | `创建 HNSW 向量索引，distance 用 l2` | `create_vector_index` | 索引创建成功 |
| 3 | `统计表里有多少条记录` | `count` | `rows=[["3"]]` |
| 4 | `查询最相似的10条向量` | `vector_search` | 返回 `1、3、2` |
| 5 | `做范围查询 where id > 100` | `range_query` | SQL 正确，本环境该范围无数据 |

测试向量：ID 1 = `[1,0,...]`，ID 2 = `[0,1,0,...]`，ID 3 = `[0.9,0.1,0,...]`（各 128 维），种子与查询结果见 [`evidence/03-seed.json`](./evidence/03-seed.json)、[`evidence/00-summary.json`](./evidence/00-summary.json)。

重复运行建表或建索引样例会因对象已存在返回错误信息（Workflow 仍完成）；新环境复现时先准备空库或更换表名。

## 当前限制

- 桥接只放行 `SELECT / SHOW / DESC / DESCRIBE / WITH / EXPLAIN / CREATE`，不放行 `INSERT / UPDATE / DELETE / DROP`。
- 嵌入式驱动 cursor 不提供列描述，`columns` 返回 `[]`，行值统一序列化为字符串。
- SQL 生成是固定关键词规则，不是通用 NL2SQL；表名插值与访问控制未达生产级安全要求。
- 桥接不可用时 Workflow 状态为 `partial-succeeded`（Dify 对默认值降级的标记），输出仍包含 SQL。
- Dify 自身使用 PostgreSQL/pgvector；seekdb 由 Workflow 通过桥接实际调用，未替代 Dify 元数据库。
