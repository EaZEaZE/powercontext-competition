# Dify 赛题 · OceanBase SQL Helper

## 一句话方案

在 Dify 中用 **Chatflow** 编排「自然语言 → OceanBase / seekdb SQL → seekdb 实际执行 → 结果回显」，
并把 seekdb 同时作为 Dify 的**元数据库**和**向量数据库**。

## 交付物对照

| 赛题要求 | 交付内容 |
|---|---|
| Dify DSL .yml（含全部 DSL） | [`dsl/oceanbase-sql-helper.yml`](./dsl/oceanbase-sql-helper.yml) |
| DSL 用途 | 见下方「DSL 结构与用途」 |
| 导入顺序 | 见下方「导入顺序」 |
| 运行说明 | 见下方「部署 Dify」「启动 seekdb 桥接」 |
| 验证样例 | 见下方「验证样例」 |
| 实际使用 seekdb | Dify 元数据库 + 向量库均为 seekdb；Chatflow 通过桥接服务真实执行 SQL |

## 文件结构

```
dify/
├── .env                        # seekdb 同时作为 MetaDB + VectorStore 的配置
├── deploy.sh                   # 一键部署 Dify + seekdb
├── dsl/
│   └── oceanbase-sql-helper.yml   # Dify 应用 DSL（Chatflow）
└── bridge/
    ├── seekdb_bridge.py        # 把 SQL 落到 seekdb 执行的桥接服务
    └── requirements.txt
```

## DSL 结构与用途

应用模式：**Chatflow**（`advanced-chat`），4 个业务节点：

| 节点 | 类型 | 作用 |
|---|---|---|
| 开始 | start | 收集「需求描述 / 表名 / 向量维度 / 返回条数」四个入参 |
| 生成 SQL | llm | 用内置的 OceanBase 4.x / seekdb 语法模板生成一条 SQL，只输出 SQL |
| 清洗 SQL | code | 剥离 ```sql 围栏，规范化分号，判定是否为只读语句 |
| seekdb 执行 | http-request | POST `{{SEEKDB_BRIDGE_URL}}/sql` 真实执行，服务不可用时降级为仅展示 SQL |
| 回答 | answer | 回显 SQL + seekdb 返回的 JSON + 只读校验标记 |

环境变量 `SEEKDB_BRIDGE_URL` 默认 `http://host.docker.internal:18080`，可在 Dify「环境变量」中改成实际地址。

## 导入顺序

1. 部署 Dify（下节），完成管理员初始化。
2. 在「设置 → 模型供应商」配置任意 OpenAI 兼容模型，并替换 DSL 里的 `llm` 节点模型
   （默认写的是通义 `qwen-plus`，换成本实例已配置的模型即可）。
3. 启动 seekdb 桥接服务（下节），确认 `GET /health` 返回 `{"ok": true}`。
4. Dify「工作室 → 导入 DSL 文件」，选择 `dsl/oceanbase-sql-helper.yml`。
5. 打开应用，在「环境变量」里确认 `SEEKDB_BRIDGE_URL` 指向桥接服务。
6. 发布应用，用「验证样例」逐条验证。

> 只有一个 DSL 文件，不存在多文件依赖，导入顺序即：先 Dify/模型/桥接，再导入 DSL。

## 部署 Dify × seekdb

前置条件：Docker & Docker Compose。

```bash
git clone --depth 1 -b v1.10.1 https://github.com/langgenius/dify.git
cd dify/docker
cp /path/to/dify/.env .env   # 本目录的 .env：seekdb 同时作为 MetaDB + VectorStore
docker compose up -d
docker logs -f docker-api-1  # 看到 "Database migration successful!" 即成功
```

关键配置（`.env`）：

```env
DB_TYPE=mysql
DB_HOST=seekdb
DB_PORT=2881
DB_DATABASE=test

VECTOR_STORE=oceanbase
OCEANBASE_VECTOR_HOST=seekdb
OCEANBASE_VECTOR_PORT=2881
OCEANBASE_VECTOR_DATABASE=test

COMPOSE_PROFILES=seekdb
```

初始化管理员：`http://<host>/install`。

## 启动 seekdb 桥接

```bash
cd bridge
pip install -r requirements.txt
SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 SEEKDB_USER=root SEEKDB_PASSWORD= \
  uvicorn seekdb_bridge:app --host 0.0.0.0 --port 18080

curl http://127.0.0.1:18080/health
# {"ok":true,"seekdb":"127.0.0.1:2881"}
```

桥接只放行 `SELECT / SHOW / DESC / DESCRIBE / WITH / EXPLAIN`，
DDL 与 DML 会返回「仅允许只读语句」，与 SKILL 规范「生成 DDL 先给人确认」一致。

## 验证样例

| # | 输入 | 期望 SQL | seekdb 执行结果 |
|---|---|---|---|
| 1 | 需求=`帮我建个128维的向量表`，表名=`items` | `CREATE TABLE items (id BIGINT PRIMARY KEY, v VECTOR(128));` | `{"ok":false,"message":"仅允许只读语句..."}`（DDL 需人工确认后执行） |
| 2 | 需求=`创建 HNSW 向量索引，distance 用 l2`，表名=`items` | `CREATE VECTOR INDEX idx_items_v ON items(v) WITH (distance=l2, type=hnsw, ...);` | 同上，只读保护 |
| 3 | 需求=`查询最相似的10条向量`，表名=`items` | `SELECT id, l2_distance(v, query_vector) AS distance FROM items ORDER BY l2_distance(v, query_vector) APPROXIMATE LIMIT 10;` | 返回 `columns/rows`，`row_count` ≥ 0 |
| 4 | 需求=`统计表里有多少条记录`，表名=`items` | `SELECT COUNT(*) AS total FROM items;` | `{"ok":true,"columns":["total"],"rows":[["0"]],"row_count":1}` |

样例 3、4 可直接在 Dify 对话框里复现；样例 1、2 验证「生成 SQL + 只读保护」两件事。

## 复现清单

1. `docker compose up -d` 起 Dify + seekdb，迁移成功。
2. `uvicorn seekdb_bridge:app --port 18080`，`/health` 通。
3. 导入 `dsl/oceanbase-sql-helper.yml`，模型节点换成实例已配置模型。
4. 跑通上表 4 条样例。
