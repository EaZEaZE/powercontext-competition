# 阶跃星辰赛题 · 端侧 AI 推理与 seekdb 混合检索

## 一句话方案

浏览器本地用 **Transformers.js + INT8 量化 all-MiniLM-L6-v2** 计算文档与查询的 embedding，**seekdb** 负责存储与向量+全文混合检索（RRF 融合），全程无云端推理调用。

## 技术架构

```
┌───────────────────────── 浏览器 ─────────────────────────┐
│  Transformers.js (WebGPU → WASM 自动降级)                 │
│  ├─ all-MiniLM-L6-v2 INT8 量化模型                        │
│  ├─ 文本切块 → 本地 embedding                              │
│  └─ 查询 → 本地 embedding                                  │
└──────────────────────┬───────────────────────────────────┘
                       │ HTTP (embeddings 向量)
┌──────────────────────▼───────────────────────────────────┐
│  Node.js 后端 (seekdb-js)                                  │
│  ├─ POST /api/add   → collection.add(embeddings)          │
│  └─ POST /api/search → collection.hybridSearch(RRF)       │
└──────────────────────┬───────────────────────────────────┘
                       │ MySQL 协议 (2881)
┌──────────────────────▼───────────────────────────────────┐
│  seekdb                                                   │
│  ├─ FulltextIndex (全文)                                   │
│  ├─ VectorIndex HNSW cosine 384d (embeddingFunction=null) │
│  └─ SparseVectorIndex BM25                                │
└──────────────────────────────────────────────────────────┘
```

## 模型量化对比

| 项 | FP32 (model.onnx) | INT8 (model_quantized.onnx) |
|---|---|---|
| 模型体积 | ~90 MB | ~23 MB |
| 压缩比 | 1x | **~3.9x** |
| 推理后端 | WASM/WebGPU | WASM/WebGPU |
| 检索质量（余弦相似度偏差） | 基线 | ≤ 0.01 |

> 数据来自 Transformers.js 官方提供的 all-MiniLM-L6-v2 量化版本。INT8 通过 8 位整数量化权重，精度损失极小，体积缩小约 4 倍。

## 推理后端策略

```js
env.backends.onnx.wasm.numThreads = 4;
extractor = await pipeline("feature-extraction", "Xenova/all-MiniLM-L6-v2", {
  quantized: true, // INT8
});
```

- **优先 WebGPU**：利用 GPU 并行加速矩阵运算
- **自动降级 WASM**：浏览器不支持 WebGPU 时回退到 WebAssembly
- UI 实时显示当前后端类型

## seekdb 混合检索

embedding 由浏览器计算后传入，seekdb 不做向量化：

```js
const schema = new Schema()
  .createIndex(new FulltextIndexConfig())
  .createIndex(new VectorIndexConfig({
    hnsw: { dimension: 384, distance: "cosine" },
    embeddingFunction: null, // 由浏览器提供
  }))
  .createIndex(new SparseVectorIndexConfig({
    sourceKey: K.DOCUMENT,
    embeddingFunction: new Bm25EmbeddingFunction(),
  }));
```

检索由 seekdb 完成 RRF 融合，客户端不合并排序：

```js
collection.hybridSearch({
  query: { whereDocument: { $contains: queryText }, nResults: 10 },  // 全文
  knn:   { queryEmbeddings: [queryEmbedding], nResults: 10 },         // 向量
  rank:  { rrf: {} },
  nResults: 5,
});
```

## 性能数据（实测后填写）

| 指标 | 优化前 | 优化后 |
|---|---|---|
| 模型加载耗时 | - | - |
| 单条 embedding 延迟 | - | - |
| 批量 8 条 embedding 延迟 | - | - |
| seekdb 混合检索延迟 | - | - |

## 运行方式

```bash
# 1. 启动 seekdb
docker compose up -d

# 2. 安装依赖并启动后端
cd backend && npm install && npm start

# 3. 浏览器访问
open http://localhost:3000
```

## 文件结构

```
stepfun/
├── docker-compose.yml      # seekdb 服务
├── backend/
│   ├── package.json
│   └── server.js           # seekdb-js REST 代理
└── frontend/
    └── index.html          # Transformers.js + UI
```
