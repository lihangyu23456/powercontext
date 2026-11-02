# MiniMax × OceanBase 数据库助手（MCP Server）

让 **MiniMax 模型**通过 MCP 协议直接诊断并修复 **seekdb / OceanBase** 上的问题，
并通过 **PowerContext** 检索记忆。

- 赛题：MiniMax（20 分）
- 形态：MCP Server（stdio），任何 MCP 客户端都能挂载
- 提交位置：`minimax/` 目录（基于 `2026-competition-ecosystem` 分支）

---

## 一、赛题要求与本实现的对应

| 赛题要求 | 本实现 |
|---|---|
| 数据库结构概览 | 工具 `database_overview`：一次返回所有表、列、主键/索引、行数估算 |
| 性能/配置数据获取 | 工具 `run_query`（只读 SQL）、`collect_metrics`（运行变量 + 状态计数器） |
| 使用 MiniMax 模型 | 工具 `minimax_advise`：把问题与上下文发给 MiniMax，返回结构化修复方案 |
| 自动生成修复建议 | `minimax_advise` 返回 `diagnosis / evidence / statements / verification / risk` |
| 应用建议并验证修复 | 工具 `apply_fix`（逐条执行）与 `verify_fix`（重跑校验语句） |
| PowerContext 集成 | 工具 `powercontext_search`：检索指定 scope 的记忆 |
| MiniMax 调用记录 + 提示词 | 每次模型调用写入 `logs/minimax_calls.jsonl`（含完整 system/user prompt 与原始响应） |
| 状态日志 | 每个工具调用写入 `logs/status.log`（工具名、状态、耗时上下文） |

---

## 二、架构

```
┌──────────────────┐   MCP (stdio, JSON-RPC)   ┌───────────────────────────────────┐
│  MiniMax 模型     │ ───────────────────────▶ │  minimax_mcp.server                │
│  (或任意 MCP 客户端)│                          │  ├─ database_overview               │
└──────────────────┘                          │  ├─ run_query / collect_metrics     │
                                              │  ├─ minimax_advise ──▶ MiniMax API  │
                                              │  ├─ apply_fix / verify_fix           │
                                              │  └─ powercontext_search ──▶ PC      │
                                              └───────┬───────────────┬───────────────┘
                                                      │               │
                                        MySQL 协议    │               │ HTTP API
                                                      ▼               ▼
                                            ┌──────────────┐  ┌────────────────┐
                                            │ seekdb 实例   │  │ PowerContext   │
                                            │ (数据与索引)  │  │ (记忆与检索)    │
                                            └──────────────┘  └────────────────┘
```

**选型理由**

| 决策 | 理由 |
|---|---|
| MCP stdio 而不是 HTTP | MCP 客户端（含 MiniMax 官方接入方式）默认拉起子进程，stdio 零配置、零端口冲突 |
| `mcp` 官方 Python SDK | 题面要求 MCP 协议；官方 SDK 的 `Server`/`Tool` 与客户端生态兼容，无需自研协议 |
| seekdb 走 MySQL 协议 | 题面要求「不能使用其他数据库」；seekdb 兼容 MySQL，用 `pymysql` 直连 socket，不引入 ORM |
| PowerContext 走官方 Python Client | 题面要求「提供记忆与检索能力」；复用官方 `PowerContextClient`，不自己拼 HTTP |
| 模型调用走 OpenAI 兼容的 `/chat/completions` | MiniMax 提供 OpenAI 兼容端点，客户端只依赖 `httpx`，换模型只改 `MINIMAX_MODEL` |
| 写操作默认关闭 | `MINIMAX_MCP_ALLOW_WRITES=1` 才放开 `apply_fix` 的写语句，避免模型误改生产库 |
| 模型输出按 JSON 解析并容错 | 题面要求「自动生成修复建议」；要求模型只输出 JSON，同时容忍围栏代码块 |

---

## 三、目录结构

```
minimax/
├── README.md                    本文件：架构、选型、运行方式、验证结论
├── requirements.txt             依赖
├── docs/
│   └── one-page.md              一页纸说明：架构图、选型、实测结果
├── minimax_mcp/
│   ├── __init__.py
│   ├── __main__.py              python -m minimax_mcp.server
│   ├── config.py                环境变量配置
│   ├── clients.py               seekdb / PowerContext / MiniMax 三个客户端（含调用留痕）
│   ├── tools.py                 工具实现：结构概览、指标、模型建议、修复、验证、检索
│   └── server.py                MCP Server（工具声明 + 分发）
├── verify/
│   ├── run_verification.sh      端到端验证脚本（一条命令）
│   ├── verify_end_to_end.py     端到端验证主体
│   ├── mcp_smoke_test.py        以真实 MCP 客户端连上 Server 并调用工具
│   ├── offline_minimax.py       离线的确定性模型桩，保证验证可复现
│   ├── prepare_demo_data.sh     造演示数据
│   └── logs/                    验证产物：模型调用记录、提示词、状态日志
└── tests/
    └── test_tools.py            工具层自测
```

---

## 四、运行方式

### 4.1 依赖

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# PowerContext SDK：已发布则直接装；用本地仓库则
pip install powercontext            # 或 export PYTHONPATH=/path/to/powercontext/src
```

### 4.2 配置（全部走环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MINIMAX_API_KEY` | 空 | MiniMax 的 API Key，真实调用必填 |
| `MINIMAX_BASE_URL` | `https://api.minimaxi.com/v1` | MiniMax 的 OpenAI 兼容端点 |
| `MINIMAX_MODEL` | `MiniMax-M2` | 模型名 |
| `MINIMAX_MCP_SEEKDB_SOCKET` | `/home/hsx/seekdb/run/sql.sock` | seekdb 的 unix socket |
| `MINIMAX_MCP_SEEKDB_HOST` / `_PORT` | `127.0.0.1` / `2881` | 用 TCP 连 seekdb 时使用 |
| `MINIMAX_MCP_SEEKDB_DATABASE` | `oceanbase` | 连接的库 |
| `MINIMAX_MCP_POWERCONTEXT_URL` | `http://127.0.0.1:8000` | PowerContext 服务地址 |
| `MINIMAX_MCP_POWERCONTEXT_TOKEN` | 空 | PowerContext 的 Bearer Token |
| `MINIMAX_MCP_ALLOW_WRITES` | `0` | 置 `1` 才允许 `apply_fix` 写库 |
| `MINIMAX_MCP_LOG_DIR` | `logs` | 调用记录与状态日志目录 |

### 4.3 启动

```bash
# 作为 MCP Server 被客户端拉起（stdio）
python3 -m minimax_mcp.server

# 手工挂到任意 MCP 客户端：
#   claude_desktop_config.json / mcp.json 里加入
#   { "mcpServers": { "minimax-db": { "command": "python3", "args": ["-m", "minimax_mcp.server"] } } }
```

### 4.4 端到端验证

```bash
# 离线可复现（不需要 Key 和外网）
bash verify/run_verification.sh

# 真实模型
MINIMAX_API_KEY=<你的 Key> bash verify/run_verification.sh
```

验证脚本会依次完成：结构概览 → 取配置与性能数据 → 让 MiniMax 给修复方案 →
应用修复 → 验证修复 → 通过 PowerContext 检索记忆，并写出全部日志。

---

## 五、验证结论

### 5.1 端到端（`logs/`）

| 步骤 | 结果 |
|---|---|
| 结构概览 | 返回 `orders` 表及其列、主键、行数 |
| 配置与性能数据 | 取到 5 个运行变量与状态计数器 |
| MiniMax 修复建议 | 产出 diagnosis / evidence / statements / verification / risk 五段结构 |
| 应用修复 | 修复语句执行成功（索引建成） |
| 验证修复 | 校验语句全部成功，行为与修复前一致（行数不变） |
| PowerContext 检索 | 命中并返回记忆条目 |
| MCP 协议 | 以真实 MCP 客户端完成 initialize → list_tools → call_tool，7 个工具全部可见 |

### 5.2 模型调用记录

`logs/minimax_calls.jsonl` 每行一次调用，包含：工具名、模型名、端点、
**完整 system prompt 与 user prompt**、请求体、HTTP 状态、原始响应、耗时。
`logs/status.log` 记录每个工具的状态流转。

> 提交前请用真实 Key 再跑一次 `MINIMAX_API_KEY=... bash verify/run_verification.sh`，
> 让日志里的 `endpoint` 指向真实的 MiniMax 服务；仓库里附带的是离线桩的运行记录，
> 其 prompt 与真实调用完全一致（同一段代码、同一个请求体）。

---

## 六、已知边界

- `apply_fix` 只执行模型给出的 SQL，不做语义校验，**生产使用请务必先在只读副本上验证**；
  默认 `MINIMAX_MCP_ALLOW_WRITES=0` 就是为此。
- 模型建议的 SQL 质量取决于模型与上下文，工具层保证的是「结构化、可执行、可验证」，
  不保证建议一定最优。
- `powercontext_search` 需要一个可连接的 PowerContext 实例；未启动时该工具返回错误，
  其余工具不受影响。
