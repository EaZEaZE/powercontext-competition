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

# 2. 配置环境变量
export MINIMAX_API_KEY="你的MiniMax Key"
export POWERCONTEXT_BASE="http://127.0.0.1:8001"
export SEEKDB_HOST="127.0.0.1"   # seekdb 服务器地址
export SEEKDB_PORT="2881"         # seekdb 端口

# 3. 启动
python main.py
# 访问 http://localhost:8000/static/index.html
```

## 核心接口

| 接口 | 说明 |
|---|---|
| `POST /api/import` | 导入文档（切块→向量化→写入 seekdb） |
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
│   └── requirements.txt
├── frontend/
│   └── index.html         # 问答 UI
└── README.md
```
