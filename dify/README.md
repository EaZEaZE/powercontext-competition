# Dify 赛题 · OceanBase SQL Helper

## 一句话方案

在 Dify 中用 **Workflow** 编排「自然语言需求 → 规则生成 OceanBase / seekdb SQL → seekdb 实际执行 → 结果输出」。主流程不依赖外部模型或模型 Key，导入后即可运行；seekdb 桥接不可用时自动降级为仅返回 SQL，不阻断 Workflow。

## 交付物对照

| 赛题要求 | 交付内容 |
|---|---|
| Dify DSL .yml（含全部 DSL） | [`dsl/oceanbase-sql-helper.yml`](./dsl/oceanbase-sql-helper.yml) |
| DSL 用途 | 见下方「DSL 结构与用途」 |
| 导入顺序 | 见下方「导入顺序」 |
| 运行说明 | 见下方「部署 Dify」「启动 seekdb 桥接」 |
| 验证样例 | 见下方「验证样例」 |
| 实际使用 seekdb | Dify 可配置 seekdb 为 MetaDB + VectorStore；Workflow 通过桥接服务执行 SQL |

## 文件结构

```
dify/
├── .env                        # seekdb 同时作为 MetaDB + VectorStore 的配置
├── deploy.sh                   # 部署 Dify + seekdb
├── dsl/
│   └── oceanbase-sql-helper.yml # Dify Workflow DSL
└── bridge/
    ├── seekdb_bridge.py        # 把 SQL 落到 seekdb 执行的桥接服务
    └── requirements.txt
```

## DSL 结构与用途

应用模式：**Workflow**（`mode: workflow`），主流程为：

| 节点 | 类型 | 作用 |
|---|---|---|
| 开始 | start | 接收「需求描述 / 表名 / 向量维度 / 返回条数」四个可选入参 |
| 生成 SQL | code | 按需求关键词生成建表、向量索引、相似度检索、统计或范围查询 SQL |
| seekdb 执行 | http-request | POST `{{SEEKDB_BRIDGE_URL}}/sql` 执行 SQL；失败时使用默认值，不阻断主流程 |
| 结果输出 | end | 输出 `sql`、`kind` 和 `seekdb_result` |

DSL 无模型插件 `dependencies`，不需要配置通义、OpenAI 或其他模型供应商。环境变量 `SEEKDB_BRIDGE_URL` 默认 `http://host.docker.internal:18080`，可在 Dify「环境变量」中改成实际地址。

## 导入顺序

1. 部署并初始化 Dify。
2. Dify「工作室 → 导入 DSL 文件」，选择 `dsl/oceanbase-sql-helper.yml`。
3. 如需实际执行 SQL，先启动 seekdb 桥接服务，再把 `SEEKDB_BRIDGE_URL` 设置为桥接地址。
4. 发布应用，在 Workflow 运行页填写「需求描述」等入参并运行。
5. 用下方「验证样例」逐条检查输出。

> 只有一个 DSL 文件，不存在多文件依赖，也不需要先安装模型插件。桥接服务仅影响实际 SQL 执行，不影响 SQL 生成和 Workflow 完成。

## 部署 Dify × seekdb

前置条件：Docker & Docker Compose。

```bash
git clone --depth 1 -b v1.10.1 https://github.com/langgenius/dify.git
cd dify/docker
cp /path/to/dify/.env .env
docker compose up -d
docker logs -f docker-api-1
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

桥接只放行 `SELECT / SHOW / DESC / DESCRIBE / WITH / EXPLAIN`。DDL 与 DML 会返回「仅允许只读语句」，用于避免未经确认直接修改数据库。

## 验证样例

以下样例均使用默认表名 `items`；即使桥接未连接，也应正常完成 Workflow 并返回 SQL。

| # | 需求描述 | 期望 `kind` | 期望 SQL 关键内容 |
|---|---|---|---|
| 1 | `帮我建个128维向量表` | `create_table` | `CREATE TABLE items (id BIGINT PRIMARY KEY, v VECTOR(128));` |
| 2 | `创建 HNSW 向量索引，distance 用 l2` | `create_vector_index` | `CREATE VECTOR INDEX ... WITH (distance=l2, type=hnsw, ...)` |
| 3 | `查询最相似的10条向量` | `vector_search` | `ORDER BY l2_distance(v, query_vector) APPROXIMATE LIMIT 10;` |
| 4 | `统计表里有多少条记录` | `count` | `SELECT COUNT(*) AS total FROM items;` |
| 5 | `做范围查询 where id > 100` | `range_query` | `WHERE id > 100 AND id < 1000 LIMIT 100;` |

桥接连接成功后，样例 3、4、5 会在 `seekdb_result` 中返回实际查询结果；样例 1、2 返回只读保护信息。

## 复现清单

1. 导入 `dsl/oceanbase-sql-helper.yml`，无需配置模型。
2. 直接运行样例 1，确认能完成 Workflow 并输出建表 SQL。
3. 启动 seekdb 与桥接服务，确认 `/health` 返回正常。
4. 配置 `SEEKDB_BRIDGE_URL`，再运行样例 3、4、5，检查实际执行结果。
