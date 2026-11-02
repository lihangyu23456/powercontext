# 端侧知识库：本地向量化 + seekdb 混合检索

> 赛题：阶跃星辰（20 分）｜作品：浏览器里完成向量化，检索交给 seekdb

## 1. 作品与玩法

一个纯前端的迷你知识库：

1. 用户在浏览器里粘贴一批文本；
2. 点击「本地向量化并写入 seekdb」——**向量化全部在本机完成**（Transformers.js，
   优先 WebGPU，不可用时自动降级到 WASM），原始文本与向量一起写入 seekdb；
3. 输入查询词，浏览器本地算出查询向量，交给 seekdb 做**混合检索**
   （全文关键词条件 + 向量相似度，由数据库统一排序），返回命中的原文。

## 2. 技术结合与分工

| 组件 | 承担什么 |
|---|---|
| 浏览器（Transformers.js） | **端侧推理**：文档向量化与查询向量化，全部在用户设备上完成，不调用任何云端推理 API |
| seekdb（seekdb-js） | **存储与检索**：向量、原文与元数据存在 seekdb；混合检索由 seekdb 执行，不在客户端拼合两路结果 |
| 本地 Node 服务 | 只做转发：接收浏览器算好的向量写入 seekdb，并把检索请求交给 seekdb |

检索之所以不放在浏览器里做，是因为**混合检索的排序必须在数据库里完成**：
关键词条件与向量相似度由 seekdb 的执行引擎统一打分，客户端拿到的就是最终排名。

## 3. 目录

```
stepfun/
├── README.md                    本文件：运行方式、验证样例
├── docs/technical-note.md       一页纸技术说明（模型、量化、后端、数据层、实测）
├── web/                         浏览器端（零构建，原生 ES module）
│   ├── index.html
│   ├── app.js
│   └── style.css
├── service/                     本地服务（Node + seekdb-js）
│   ├── package.json
│   └── server.mjs
└── measure/                     量化与性能实测
    ├── package.json
    ├── measure.mjs
    ├── measure.sh
    └── report.json              实测产物
```

## 4. 运行方式

### 4.1 依赖

| 组件 | 版本 | 说明 |
|---|---|---|
| 浏览器 | Chrome / Edge 最新版 | 需要 WebGPU 或 WASM |
| Node.js | ≥ 20 | 只跑本地服务与实测脚本 |
| seekdb | 1.4.0 | Client/Server 模式，`127.0.0.1:2881`，用户 `root`、空密码 |
| seekdb-js | ^1.2.0 | `npm install seekdb`（服务端模式，纯客户端，无需原生插件） |
| 模型 | `Xenova/all-MiniLM-L6-v2` | 公开可获取，Apache-2.0；INT8 与 FP32 两份 ONNX 均在模型仓库中 |

### 4.2 启动

```bash
# 1) seekdb
docker run --name seekdb -p 2881:2881 -d oceanbase/seekdb:1.4.0

# 2) 本地服务（写入 seekdb + 执行混合检索）
cd service
npm install
npm start                     # 默认 http://127.0.0.1:8010
# 可用环境变量：SEEKDB_HOST / SEEKDB_PORT / SEEKDB_USER / SEEKDB_PASSWORD /
#              SEEKDB_DATABASE / SEEKDB_COLLECTION / SEEKDB_DIMENSION / KB_SERVICE_PORT

# 3) 浏览器
cd ../web
python3 -m http.server 5173    # 或任意静态服务器
# 打开 http://127.0.0.1:5173/
```

模型由浏览器从 Hugging Face CDN 拉取并缓存在本机（`env.allowLocalModels = false`，
只用 CDN，不把模型放到服务端）。若内网无法访问 CDN，可把模型目录放到
`web/models/` 并把 `env.allowLocalModels` 改为 `true`、`env.localModelPath` 指向该目录。

### 4.3 实测脚本

```bash
cd measure
npm install
bash measure.sh                 # 输出 report.json
```

脚本会下载同一模型的 FP32 与 INT8 两份 ONNX，报告：模型文件体积与压缩比、
INT8 与 FP32 在同一评测集上的 Recall@3、量化前后查询向量的余弦相似度、
以及本地向量化耗时。`docs/technical-note.md` 里的表格来自这次运行。

## 5. 验证样例

### 样例一：本地向量化并入库

- 初始条件：seekdb 已启动；本地服务已启动；页面已打开。
- 操作：点击「本地向量化并写入 seekdb」，观察状态栏。
- 预期：状态栏出现「本地向量化 XX ms · 已写入 seekdb」；页面上方的后端徽标显示
  `WebGPU · INT8` 或 `WASM · INT8`；浏览器 Network 面板里**没有**任何推理请求
  （只有本机静态资源与 `127.0.0.1:8010` 的两次 POST）。
- 证据：`measure/report.json`；seekdb 侧可查 `SELECT COUNT(*) FROM documents`。

### 样例二：混合检索由数据库完成

- 操作：输入 `怎么让混合检索更快`，点击检索。
- 预期：结果里既有语义相近的句子，也有含关键词的句子；计时行分别给出
  「本地查询向量」「seekdb 混合检索」「端到端」三段耗时。
- 证据：计时行；本地服务日志里只有一次 `hybridSearch` 调用（不是两次结果再拼接）。

### 样例三：降级路径

- 操作：在不支持 WebGPU 的浏览器（或禁用 WebGPU）打开页面。
- 预期：后端徽标显示 `WASM · INT8`，核心流程（导入 + 检索）仍可用。

### 样例四：还原初始状态

```bash
# 删除知识库集合（在 seekdb 中）
DROP TABLE documents;
# 或直接重建 seekdb 实例
```

## 6. 已知限制

- 浏览器端只能通过本地服务访问 seekdb（seekdb-js 是 TCP/原生客户端，浏览器不能直连），
  因此服务必须与页面同机或同网段运行。
- 模型首次加载需要下载（FP32 约 90MB、INT8 约 23MB 量级，见技术说明的实测值），
  之后的推理在本地完成。
- 增量导入是逐批 `add`，索引刷新策略由 seekdb 决定；应用在写入返回后立即可检索，
  若发现新内容未命中，执行一次索引刷新再检索。
