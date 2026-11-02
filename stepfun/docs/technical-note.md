# 端侧知识库 —— 一页纸技术说明

> 赛题：阶跃星辰（20 分）｜作品：浏览器本地向量化 + seekdb 混合检索

## 问题与方案

浏览器里跑 embedding 模型，可以让用户在**自己的设备**上完成文档向量化，
再把向量、原文与元数据写进 seekdb，检索由 seekdb 的混合检索完成。

```
浏览器（Transformers.js）                  本地 Node 服务              seekdb
┌───────────────────────────┐   POST /documents  ┌──────────────┐   写入向量+原文
│ 文档 → 本地 embedding      │ ─────────────────▶ │  seekdb-js   │ ─────────────▶
│ 查询 → 本地 embedding      │ ─────────────────▶ │              │                │
│ 显示结果（带距离）          │ ◀───────────────── │  hybridSearch │ ◀─────────────
└───────────────────────────┘   POST /search     └──────────────┘   混合检索+排序
        ▲
   WebGPU 优先，不可用降级 WASM
```

## 模型与量化

| 项 | 值 |
|---|---|
| 模型 | `Xenova/all-MiniLM-L6-v2`（公开可获取，Apache-2.0） |
| 向量维度 | 384 |
| 量化方案 | **INT8**（ONNX `model_quantized.onnx`，transformers.js `dtype: 'q8'`） |
| 对照组 | FP32（ONNX `model.onnx`，`dtype: 'fp32'`） |
| 推理位置 | **全部在用户设备**：文档向量化与查询向量化都在浏览器里完成 |

体积与质量对比见下方「实测」。

## 端侧运行与降级

| 环节 | 做法 |
|---|---|
| 运行时 | Transformers.js v3（WASM + WebGPU），模型从 CDN 拉取后缓存在浏览器本地 |
| 后端选择 | 先尝试 `{ device: 'webgpu', dtype: 'q8' }`，抛错则退到 `{ device: 'wasm', dtype: 'q8' }` |
| 界面提示 | 页面顶部常驻后端徽标（`推理后端：WebGPU · INT8` / `WASM · INT8`），不可用时显示红色徽标 |
| 离线模型 | 把模型目录放到 `web/models/`，设 `env.allowLocalModels = true` 与 `env.localModelPath` 即可完全离线 |
| 批量 | 文档按批送入 extractor（一次 `pipeline(texts)`），避免逐条调用 |

**降级路径是功能的一部分**：WASM 后端下导入与检索流程必须照常跑通，页面只在徽标上
如实区分后端，不改变功能。

## seekdb 数据层

| 组成 | 说明 |
|---|---|
| 集合 | seekdb-js 的 `documents` 集合；服务端模式连接 `127.0.0.1:2881` |
| 索引 | `VectorIndexConfig`（HNSW，384 维，cosine）+ `FulltextIndexConfig`（ik 分词器，smart 模式） |
| 向量化归属 | 集合配置 `embeddingFunction: null` —— 向量由浏览器算好后随文档一起写入，服务端不再二次向量化 |
| 检索 | `collection.hybridSearch({ query: { whereDocument: { $contains } }, knn: { queryEmbeddings }, nResults })` |
| 为什么不在前端拼合 | 混合检索的关键词条件与向量相似度由 seekdb 执行引擎统一打分排序（`_score`），前端只拿最终排名 |

实测（本地 seekdb 1.4.0）里同一张表上，关键词命中的文档 `_score` 为 2.07，
纯向量命中的为 1.00 —— 排序确实由数据库给出，前端没有把两路结果自行拼合。

## 状态与增量写入

- 每次导入调用一次 `collection.add({ ids, documents, embeddings, metadatas })`，是 seekdb 的
  事务写入，写完即可检索。
- 新导入的内容若未立即命中，执行一次索引刷新后再检索；README 的验证样例给出了
  「写入 → 检索」的检查顺序。
- 前端维护集合名、当前文档条数与后端状态，切换查询时只重发查询向量，不重新导入。

## 实测

由 `measure/measure.sh` 产出 `measure/report.json`（同一评测集、同一台机器、只换量化方案）。
实测环境：Windows + Node.js v24（CPU 后端），seekdb 1.4.0 本地实例，模型 `Xenova/all-MiniLM-L6-v2`。

**模型体积与压缩比**

| 方案 | ONNX 文件 | 体积 |
|---|---|---:|
| FP32 | `model.onnx` | 90,387,606 B（86.2 MiB） |
| INT8 | `model_quantized.onnx` | 22,972,370 B（21.9 MiB） |

**压缩比 = 90,387,606 / 22,972,370 = 3.93x**（同一模型、同一份权重，只换量化）。

**检索质量变化**（8 篇文档 / 5 条查询的同一评测集，Recall@3）

| 方案 | Recall@3 | 与 FP32 的差 |
|---|---:|---:|
| FP32 | 0.5714 | — |
| INT8 | 0.5714 | **0.0000** |

量化后查询向量与 FP32 查询向量的平均余弦相似度 **0.9818** —— 向量方向基本不变，
所以在这个评测集上召回没有损失。

**端侧推理耗时**（INT8，CPU 后端，8 段文档一次批处理）：**16 ms**（约 2 ms/段）。

**seekdb 检索耗时**（3 文档、1 个 HNSW 向量索引 + 2 个 ik 全文索引，本地实例，30 次取中位数）：
**median 0.997 ms / p95 1.467 ms / max 2.134 ms**。

**混合检索的排序确实由数据库给出**：同一张表上，关键词命中的文档 `_score` = 2.0686，
纯向量命中的为 1.0000，第三篇 0.8928 —— 客户端没有把两路结果自行拼合。

复现方式：

```bash
cd measure && npm install && bash measure.sh
# 国内网络可加镜像：HF_MIRROR=https://hf-mirror.com bash measure.sh
```

## 边界

- 浏览器不能直连 seekdb（seekdb-js 是 TCP/原生客户端），因此本地服务必须与页面同机或同网段。
- 模型首次加载需要下载，之后推理完全在本地；页面刷新后由浏览器缓存命中。
- 本应用只做「导入 → 检索」，不含多用户与权限模型。
