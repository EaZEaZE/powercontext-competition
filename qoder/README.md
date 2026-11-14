# 智研助手 · Qoder × OceanBase 赛题

## 项目简介

智研助手是一个面向学习与科研场景的智能问答应用。用户导入论文摘要或笔记后，可以用自然语言提问，系统通过 **seekdb 语义检索**找到相关段落，结合 **MiniMax 大模型**生成带出处的回答，并通过 **PowerContext** 记住用户偏好与历史交互。

## 技术栈

| 组件 | 作用 | 实际使用方式 |
|---|---|---|
| **Qoder** | 开发与发布 | 项目在 Qoder 中开发，通过 Qoder Sites 发布 |
| **seekdb** | 向量检索 | 文档切块后向量化存入 seekdb，查询时语义检索 Top-5 |
| **PowerContext** | 记忆与上下文 | 保存用户历史问答与偏好，下次问答自动注入 |
| **MiniMax** | 大模型 | 基于检索结果生成带引用的回答 |

## 运行方式

```bash
# 1. 安装依赖
cd backend
pip install -r requirements.txt
# 默认 minilm 后端还需要向量模型包（会连带装 torch）；只用 hash 冒烟模式可以跳过这步
pip install "sentence-transformers==2.5.1"

# 2. 配置环境变量
export MINIMAX_API_KEY="你的MiniMax Key"
export POWERCONTEXT_BASE="http://127.0.0.1:8001"
export SEEKDB_HOST="127.0.0.1"   # seekdb 服务器地址
export SEEKDB_PORT="2881"         # seekdb 端口

# 3. 启动
python main.py
# 访问 http://localhost:8000/static/index.html
```

### 嵌入式模式（不需要单独跑 seekdb 服务器）

把向量库直接落在本地目录，省去 `SEEKDB_HOST/PORT` 那一步：

```bash
cd backend
export MINIMAX_API_KEY="你的MiniMax Key"
export POWERCONTEXT_BASE="http://127.0.0.1:8001"
export SEEKDB_EMBEDDED=1              # 开启嵌入式 seekdb
export SEEKDB_PATH="../seekdb-data"   # 数据目录，可换成任意可写路径
python main.py
```

Windows 下把 `export` 换成 `set`（cmd）或 `$env:` （PowerShell）即可，变量名相同。

> `minilm` 后端下，向量模型 `all-MiniLM-L6-v2` 采用懒加载：服务启动时不加载，第一次导入或提问时才加载（首次还会下载权重），所以启动是秒级的，但首个请求会慢一些。`hash` 后端不涉及模型加载，见下文。

### 向量后端（`EMBEDDING_BACKEND`）

| 取值 | 说明 | 适用场景 |
|---|---|---|
| `minilm`（默认） | 用 `SentenceTransformer("all-MiniLM-L6-v2")` 生成 384 维语义向量 | **正式使用**。能理解同义改写，换种问法也能召回 |
| `hash` | 字符 2/3-gram 哈希成 384 维并归一化，不加载、不下载任何模型 | 仅无网络环境的冒烟测试 |

```bash
# 离线冒烟：不下载模型，只验证链路是否通
EMBEDDING_BACKEND=hash SEEKDB_EMBEDDED=1 python main.py
```

`hash` 模式不会 import `sentence_transformers`（包和模型权重都只在 minilm 第一次要向量时才加载），所以冒烟环境连 torch 都不用装。

> `hash` 只做字面匹配，**检索质量会明显下降**（问法和原文用词不同就可能召回为空），也可能把不相干的段落排到前面，因此正式使用请务必保持 `EMBEDDING_BACKEND=minilm`。两种后端生成的向量维度相同，切换后端后旧向量不再可比，建议清空知识库后重新导入。

## 冒烟验证

后端起来后，用零依赖脚本按顺序打一遍接口（任一步失败即返回非 0）：

```bash
cd backend
python verify.py                                    # 默认 http://127.0.0.1:8000
BASE_URL=http://127.0.0.1:9000 python verify.py      # 指定其他地址
```

## 核心接口

| 接口 | 说明 |
|---|---|
| `POST /api/import` | 导入文档（切块→向量化→写入 seekdb） |
| `DELETE /api/clear` | 清空知识库（删除 `zhiyan_docs` 后重建集合） |
| `POST /api/query` | 提问（seekdb 检索 → MiniMax 回答 → PowerContext 记录） |
| `GET /api/history/{user_id}` | 读取 PowerContext 中的历史交互 |
| `POST /api/preferences/{user_id}` | 设置用户偏好 |

## 验证样例

1. 导入：粘贴一段关于"机器学习应用"的文本 → 显示"已索引 N 个段落"
2. 提问："机器学习有哪些应用？" → 返回带引用编号的回答 + 来源段落
3. 历史：刷新历史区域，显示刚才的问答记录（来自 PowerContext）

## 文件结构

```
qoder/
├── backend/
│   ├── main.py            # FastAPI + seekdb + PowerContext + MiniMax
│   ├── verify.py          # 零依赖接口冒烟脚本（标准库 urllib）
│   └── requirements.txt
├── frontend/
│   └── index.html         # 问答 UI
└── README.md
```
