// The browser side of the local knowledge base.
//
// The embedding always runs on the user's device: Transformers.js loads the
// quantized model from the Hugging Face CDN, prefers WebGPU and falls back to
// WASM. Only the vectors and the text reach the local service, which writes
// them into seekdb and runs the hybrid search there.

const MODEL_ID = 'Xenova/all-MiniLM-L6-v2';
const SERVICE = window.KB_SERVICE_URL || 'http://127.0.0.1:8010';

const backendBadge = document.getElementById('backend-badge');
const modelBadge = document.getElementById('model-badge');
const embedStatus = document.getElementById('embed-status');
const timing = document.getElementById('timing');
const resultList = document.getElementById('results');

let extractor = null;
let activeBackend = 'none';

function setBadge(element, text, kind) {
  element.textContent = text;
  element.className = `badge badge-${kind}`;
}

async function loadModel() {
  setBadge(modelBadge, '模型加载中…', 'wait');
  // The dynamic import keeps the heavy runtime out of the first paint.
  const { pipeline, env } = await import(
    'https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.0.2'
  );
  env.allowLocalModels = false;

  const attempts = [
    { device: 'webgpu', dtype: 'q8', label: 'WebGPU · INT8' },
    { device: 'wasm', dtype: 'q8', label: 'WASM · INT8' },
  ];
  for (const attempt of attempts) {
    try {
      extractor = await pipeline('feature-extraction', MODEL_ID, {
        device: attempt.device,
        dtype: attempt.dtype,
      });
      activeBackend = attempt.label;
      setBadge(backendBadge, `推理后端：${attempt.label}`, 'ok');
      setBadge(modelBadge, `${MODEL_ID} 已就绪`, 'ok');
      return;
    } catch (error) {
      console.warn(`backend ${attempt.device} failed`, error);
    }
  }
  setBadge(backendBadge, '推理后端不可用', 'bad');
  setBadge(modelBadge, '模型加载失败', 'bad');
  throw new Error('no inference backend is available');
}

async function embed(texts) {
  const output = await extractor(texts, { pooling: 'mean', normalize: true });
  const [rows, dimension] = [output.dims[0], output.dims[1]];
  const data = output.data;
  const vectors = [];
  for (let index = 0; index < rows; index += 1) {
    vectors.push(Array.from(data.slice(index * dimension, (index + 1) * dimension)));
  }
  return vectors;
}

function formatDistance(value) {
  if (value === undefined || value === null) return '';
  return ` · 距离 ${Number(value).toFixed(4)}`;
}

async function embedDocuments() {
  const lines = document
    .getElementById('docs')
    .value.split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  if (!lines.length) {
    embedStatus.textContent = '请先输入至少一段文本';
    return;
  }
  embedStatus.textContent = '本地向量化中…';
  const started = performance.now();
  const vectors = await embed(lines);
  const embeddedAt = performance.now();
  embedStatus.textContent = `本地向量化 ${(embeddedAt - started).toFixed(0)} ms`;
  await postJson('/documents', {
    ids: lines.map((_, index) => `doc-${Date.now()}-${index}`),
    documents: lines,
    embeddings: vectors,
  });
  embedStatus.textContent += ` · 已写入 seekdb（本地向量化 ${(embeddedAt - started).toFixed(0)} ms）`;
}

async function search() {
  const text = document.getElementById('query').value.trim();
  if (!text) return;
  const useKeyword = document.getElementById('use-keyword').checked;
  const started = performance.now();
  const vectors = await embed([text]);
  const embeddedAt = performance.now();
  const payload = await postJson('/search', {
    queryEmbeddings: vectors[0],
    queryText: useKeyword ? text : '',
    nResults: 5,
  });
  const finished = performance.now();
  timing.textContent =
    `本地查询向量 ${(embeddedAt - started).toFixed(0)} ms · ` +
    `seekdb 混合检索 ${(finished - embeddedAt).toFixed(0)} ms · 端到端 ${(finished - started).toFixed(0)} ms`;
  render(payload);
}

function render(payload) {
  resultList.innerHTML = '';
  const items = payload.documents || payload.results || [];
  if (!items.length) {
    resultList.innerHTML = '<li class="empty">没有命中结果，换个说法再试。</li>';
    return;
  }
  for (const item of items) {
    const element = document.createElement('li');
    const text = typeof item === 'string' ? item : item.document || item.text || JSON.stringify(item);
    const distance = typeof item === 'object' ? formatDistance(item.distance) : '';
    element.innerHTML = `<span class="doc"></span><span class="dist">${distance}</span>`;
    element.querySelector('.doc').textContent = text;
    resultList.appendChild(element);
  }
}

async function postJson(path, body) {
  const response = await fetch(`${SERVICE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`${path} failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

document.getElementById('btn-embed').addEventListener('click', () => {
  embedDocuments().catch((error) => {
    embedStatus.textContent = `失败：${error.message}`;
    console.error(error);
  });
});
document.getElementById('btn-search').addEventListener('click', () => {
  search().catch((error) => {
    timing.textContent = `失败：${error.message}`;
    console.error(error);
  });
});

loadModel().catch(() => {
  /* the badge already shows the state */
});
