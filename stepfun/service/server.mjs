// The local service behind the browser knowledge base.
//
// The browser owns the inference: it sends the vectors it computed on the
// user's device. This service only stores them in seekdb through seekdb-js and
// runs the hybrid search there, so the ranking is done by the database and not
// by stitching two result sets together in the client.

import http from 'node:http';
import { SeekdbClient, Schema, VectorIndexConfig, FulltextIndexConfig } from 'seekdb';

const HOST = process.env.SEEKDB_HOST || '127.0.0.1';
const PORT = Number(process.env.SEEKDB_PORT || 2881);
const USER = process.env.SEEKDB_USER || 'root';
const PASSWORD = process.env.SEEKDB_PASSWORD || '';
const DATABASE = process.env.SEEKDB_DATABASE || 'kb_demo';
const COLLECTION = process.env.SEEKDB_COLLECTION || 'documents';
const DIMENSION = Number(process.env.SEEKDB_DIMENSION || 384);
const SERVICE_PORT = Number(process.env.KB_SERVICE_PORT || 8010);

const client = new SeekdbClient({
  host: HOST,
  port: PORT,
  user: USER,
  password: PASSWORD,
  database: DATABASE,
});

let collection = null;

async function ensureCollection() {
  if (collection) return collection;
  const schema = new Schema({
    vectorIndex: new VectorIndexConfig({
      hnsw: { dimension: DIMENSION, distance: 'cosine' },
      // The vectors arrive from the browser, so the collection must not
      // vectorize them again on the server side.
      embeddingFunction: null,
    }),
    fulltextIndex: new FulltextIndexConfig('ik', { ik_mode: 'smart' }),
  });
  collection = await client.createCollection({ name: COLLECTION, schema });
  return collection;
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (error) {
        reject(error);
      }
    });
    request.on('error', reject);
  });
}

function send(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  response.end(body);
}

async function handleDocuments(request, response) {
  const body = await readBody(request);
  const ids = body.ids || [];
  const documents = body.documents || [];
  const embeddings = body.embeddings || [];
  if (!documents.length) {
    return send(response, 400, { error: 'no documents' });
  }
  const target = await ensureCollection();
  await target.add({
    ids,
    documents,
    embeddings,
    metadatas: documents.map((_, index) => { index }),
  });
  return send(response, 200, { added: documents.length });
}

async function handleSearch(request, response) {
  const body = await readBody(request);
  const queryEmbeddings = body.queryEmbeddings;
  if (!queryEmbeddings) {
    return send(response, 400, { error: 'no query embedding' });
  }
  const target = await ensureCollection();
  const nResults = body.nResults || 5;
  const keyword = (body.queryText || '').trim();
  let results;
  if (keyword) {
    // The hybrid search runs inside seekdb: the keyword condition and the vector
    // similarity are combined and ranked by the database.
    results = await target.hybridSearch({
      query: { whereDocument: { $contains: keyword } },
      knn: { queryEmbeddings: [queryEmbeddings] },
      nResults,
    });
  } else {
    results = await target.query({
      queryEmbeddings: [queryEmbeddings],
      nResults,
    });
  }
  const documents = results.documents || [];
  const ids = results.ids || [];
  const distances = results.distances || [];
  return send(response, 200, {
    documents: documents.map((document, index) => ({
      document,
      id: ids[index],
      distance: distances[index],
    })),
  });
}

const server = http.createServer(async (request, response) => {
  try {
    if (request.method === 'POST' && request.url === '/documents') {
      return await handleDocuments(request, response);
    }
    if (request.method === 'POST' && request.url === '/search') {
      return await handleSearch(request, response);
    }
    if (request.method === 'GET' && request.url === '/health') {
      return send(response, 200, { ok: true, collection: COLLECTION });
    }
    return send(response, 404, { error: 'not found' });
  } catch (error) {
    console.error(error);
    return send(response, 500, { error: String(error && error.message ? error.message : error) });
  }
});

server.listen(SERVICE_PORT, '127.0.0.1', () => {
  console.log(`knowledge base service on http://127.0.0.1:${SERVICE_PORT}`);
  console.log(`seekdb ${HOST}:${PORT} database=${DATABASE} collection=${COLLECTION}`);
});
