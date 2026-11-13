/**
 * 阶跃星辰赛题 - 后端服务
 * 职责：通过 seekdb-js 接入 seekdb，完成向量+全文混合检索
 * 注意：embedding 由浏览器端 Transformers.js 本地计算，本服务只做存储与检索
 */
import express from "express";
import cors from "cors";
import {
  SeekdbClient,
  Schema,
  FulltextIndexConfig,
  VectorIndexConfig,
  SparseVectorIndexConfig,
  K,
} from "seekdb";
import { Bm25EmbeddingFunction } from "@seekdb/bm25";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const app = express();
app.use(cors());
app.use(express.json({ limit: "50mb" }));
app.use(express.static(path.join(__dirname, "..", "frontend")));

const SEEKDB_HOST = process.env.SEEKDB_HOST || "127.0.0.1";
const SEEKDB_PORT = parseInt(process.env.SEEKDB_PORT || "2881");
const COLLECTION_NAME = process.env.COLLECTION_NAME || "stepfun_kb";
const DIMENSION = 384; // all-MiniLM-L6-v2

let client = null;
let collection = null;

async function initSeekdb() {
  client = new SeekdbClient({
    host: SEEKDB_HOST,
    port: SEEKDB_PORT,
    user: "root",
    password: "",
    database: "test",
  });

  const bm25 = new Bm25EmbeddingFunction();
  const schema = new Schema()
    .createIndex(new FulltextIndexConfig())
    .createIndex(
      new VectorIndexConfig({
        hnsw: { dimension: DIMENSION, distance: "cosine" },
        embeddingFunction: null, // embedding 由浏览器端计算并传入
      })
    )
    .createIndex(
      new SparseVectorIndexConfig({
        sourceKey: K.DOCUMENT,
        embeddingFunction: bm25,
      })
    );

  collection = await client.getOrCreateCollection({
    name: COLLECTION_NAME,
    schema,
  });
  console.log(`[seekdb] connected, collection="${COLLECTION_NAME}"`);
}

/**
 * 导入文档：浏览器传入已计算好的 embeddings
 */
app.post("/api/add", async (req, res) => {
  try {
    const { ids, documents, metadatas, embeddings } = req.body;
    if (!documents || !embeddings) {
      return res.status(400).json({ error: "documents and embeddings required" });
    }
    await collection.add({ ids, documents, metadatas, embeddings });
    res.json({ ok: true, count: documents.length });
  } catch (e) {
    console.error("[add] error:", e.message);
    res.status(500).json({ error: e.message });
  }
});

/**
 * 混合检索：浏览器传入 query embedding，seekdb 完成向量+全文融合（RRF）
 */
app.post("/api/search", async (req, res) => {
  try {
    const { queryEmbedding, queryText, nResults = 5 } = req.body;
    if (!queryEmbedding) {
      return res.status(400).json({ error: "queryEmbedding required" });
    }

    const t0 = Date.now();
    const result = await collection.hybridSearch({
      query: {
        whereDocument: queryText ? { $contains: queryText } : undefined,
        nResults: nResults * 2,
      },
      knn: {
        queryEmbeddings: [queryEmbedding],
        nResults: nResults * 2,
      },
      rank: { rrf: {} },
      nResults,
    });
    const latency = Date.now() - t0;

    const ids = result.ids?.[0] || [];
    const docs = result.documents?.[0] || [];
    const dists = result.distances?.[0] || [];
    const metas = result.metadatas?.[0] || [];

    res.json({
      results: ids.map((id, i) => ({
        id,
        document: docs[i],
        distance: dists[i],
        metadata: metas[i],
      })),
      latencyMs: latency,
    });
  } catch (e) {
    console.error("[search] error:", e.message);
    res.status(500).json({ error: e.message });
  }
});

app.get("/api/stats", async (req, res) => {
  try {
    const count = await collection.count();
    res.json({ count, collection: COLLECTION_NAME });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.delete("/api/clear", async (req, res) => {
  try {
    await client.deleteCollection(COLLECTION_NAME);
    const bm25 = new Bm25EmbeddingFunction();
    const schema = new Schema()
      .createIndex(new FulltextIndexConfig())
      .createIndex(
        new VectorIndexConfig({
          hnsw: { dimension: DIMENSION, distance: "cosine" },
          embeddingFunction: null,
        })
      )
      .createIndex(
        new SparseVectorIndexConfig({
          sourceKey: K.DOCUMENT,
          embeddingFunction: bm25,
        })
      );
    collection = await client.createCollection({
      name: COLLECTION_NAME,
      schema,
    });
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

const PORT = process.env.PORT || 3000;
initSeekdb()
  .then(() => {
    app.listen(PORT, () => {
      console.log(`[server] listening on http://localhost:${PORT}`);
    });
  })
  .catch((e) => {
    console.error("[init] failed:", e.message);
    process.exit(1);
  });
