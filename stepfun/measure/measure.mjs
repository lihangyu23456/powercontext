// Measure the model size, the quantization effect and the local embedding cost.
//
//   node measure.mjs                 # the report on stdout
//   node measure.mjs --json out.json # also write the raw numbers
//
// The script compares the fp32 and the INT8 builds of the same model, so the
// reported compression ratio and the quality change come from the same corpus.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const MODEL_ID = 'Xenova/all-MiniLM-L6-v2';

const CORPUS = [
  { id: 'd1', text: 'seekdb 把向量检索、全文检索和结构化条件放进同一个 SQL 引擎', topic: 'hybrid' },
  { id: 'd2', text: '混合检索可以一条语句同时用上关键词匹配和向量相似度', topic: 'hybrid' },
  { id: 'd3', text: 'PowerContext 负责把值得记住的结论沉淀成记忆', topic: 'memory' },
  { id: 'd4', text: 'HNSW 索引用近似最近邻换来更低的查询延迟', topic: 'index' },
  { id: 'd5', text: 'BM25 排序对关键词检索依然有效', topic: 'keyword' },
  { id: 'd6', text: '浏览器里可以用 Transformers.js 在本地做向量化', topic: 'browser' },
  { id: 'd7', text: 'WebGPU 不可用时要降级到 WASM 后端', topic: 'browser' },
  { id: 'd8', text: '量化会改变向量，需要用同一套评测集比较召回', topic: 'quality' },
];

const QUERIES = [
  { text: '怎么让关键词和向量一起查', expect: ['d1', 'd2'] },
  { text: '记忆怎么保存', expect: ['d3'] },
  { text: '近似最近邻索引', expect: ['d4'] },
  { text: '浏览器本地推理', expect: ['d6', 'd7'] },
  { text: '量化对召回有影响吗', expect: ['d8'] },
];

function cosine(left, right) {
  let dot = 0;
  let leftNorm = 0;
  let rightNorm = 0;
  for (let index = 0; index < left.length; index += 1) {
    dot += left[index] * right[index];
    leftNorm += left[index] * left[index];
    rightNorm += right[index] * right[index];
  }
  return dot / (Math.sqrt(leftNorm) * Math.sqrt(rightNorm) || 1);
}

async function load(dtype, cacheDir) {
  const { pipeline, env } = await import('@huggingface/transformers');
  env.cacheDir = cacheDir;
  // A mirror keeps the measurement runnable where huggingface.co is blocked.
  const mirror = process.env.HF_MIRROR || process.env.HF_ENDPOINT;
  if (mirror) {
    env.remoteHost = mirror;
    env.remotePathTemplate = '{model}/resolve/{revision}/';
  }
  return pipeline('feature-extraction', MODEL_ID, { dtype, device: 'cpu' });
}

async function embedAll(extractor, texts) {
  const output = await extractor(texts, { pooling: 'mean', normalize: true });
  const [rows, dimension] = [output.dims[0], output.dims[1]];
  const vectors = [];
  for (let index = 0; index < rows; index += 1) {
    vectors.push(Array.from(output.data.slice(index * dimension, (index + 1) * dimension)));
  }
  return vectors;
}

function recallAtThree(vectors) {
  let hit = 0;
  let total = 0;
  for (const query of QUERIES) {
    const queryVector = vectors.queries[query.text];
    const ranked = CORPUS
      .map((document, index) => ({
        id: document.id,
        score: cosine(queryVector, vectors.documents[index]),
      }))
      .sort((left, right) => right.score - left.score)
      .slice(0, 3)
      .map((item) => item.id);
    for (const expected of query.expect) {
      total += 1;
      if (ranked.includes(expected)) hit += 1;
    }
  }
  return total ? hit / total : 0;
}

function collectOnnxFiles(root) {
  const files = [];
  const walk = (current) => {
    if (!fs.existsSync(current)) return;
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith('.onnx')) files.push({ file: entry.name, bytes: fs.statSync(full).size });
    }
  };
  walk(root);
  return files;
}

async function main() {
  const cacheDir = path.join(os.tmpdir(), 'kb-model-cache');
  const report = { model: MODEL_ID, measured_at: new Date().toISOString(), device: 'cpu' };

  const fp32 = await load('fp32', cacheDir);
  const int8 = await load('q8', cacheDir);
  const documents = CORPUS.map((item) => item.text);
  const queries = QUERIES.map((item) => item.text);

  const fp32Documents = await embedAll(fp32, documents);
  const fp32Queries = await embedAll(fp32, queries);
  const int8Documents = await embedAll(int8, documents);
  const int8Queries = await embedAll(int8, queries);

  report.onnx_files = collectOnnxFiles(cacheDir);

  const fp32Vectors = {
    documents: fp32Documents,
    queries: Object.fromEntries(QUERIES.map((query, index) => [query.text, fp32Queries[index]])),
  };
  const int8Vectors = {
    documents: int8Documents,
    queries: Object.fromEntries(QUERIES.map((query, index) => [query.text, int8Queries[index]])),
  };

  report.recall_at_3 = {
    fp32: recallAtThree(fp32Vectors),
    int8: recallAtThree(int8Vectors),
  };

  let agreement = 0;
  for (const query of QUERIES) {
    agreement += cosine(fp32Vectors.queries[query.text], int8Vectors.queries[query.text]);
  }
  report.quantization_query_cosine = agreement / QUERIES.length;

  const started = performance.now();
  await embedAll(int8, documents);
  report.embedding_int8_ms = Math.round(performance.now() - started);

  const jsonIndex = process.argv.indexOf('--json');
  if (jsonIndex > 0 && process.argv[jsonIndex + 1]) {
    fs.writeFileSync(process.argv[jsonIndex + 1], JSON.stringify(report, null, 2));
  }
  console.log(JSON.stringify(report, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
