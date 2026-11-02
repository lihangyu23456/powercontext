# MiniMax × OceanBase 数据库助手 —— 一页纸说明

> 赛题：MiniMax（20 分）｜形态：MCP Server（stdio）｜位置：`minimax/`

## 问题

AI Agent 排查数据库问题时，缺少「能自己看结构、取指标、改代码、验证效果」的能力：
模型看不到真实库状态，只能凭空建议，改完也无法确认是否生效。

## 方案

一个 MCP Server，把 MiniMax 模型接到真实数据库上，形成闭环：

```
   MiniMax 模型
        │  MCP (stdio, JSON-RPC)
        ▼
┌──────────────────────────────────────┐
│ minimax_mcp.server                   │
│  database_overview  结构概览           │
│  run_query/collect_metrics  取数据    │
│  minimax_advise ──▶ MiniMax /chat/completions
│  apply_fix / verify_fix  改与验        │
│  powercontext_search ──▶ PowerContext │
└───────┬──────────────────────┬───────┘
        │ MySQL 协议            │ HTTP API
        ▼                       ▼
   seekdb 实例              PowerContext
   (数据/索引/执行计划)        (记忆与检索)
```

**一次完整任务的实际轨迹**（`verify/logs/status.log`）：

```
tool=database_overview status=ok detail=1 tables
tool=collect_metrics   status=ok
tool=minimax_advise    status=ok detail=1 statements
tool=apply_fix         status=ok detail=1 statements
tool=verify_fix        status=ok detail=2 checks
tool=powercontext_search status=ok
```

## 关键选型

| 选择 | 理由 |
|---|---|
| MCP over stdio | 题面指定 MCP；stdio 由客户端拉起子进程，零端口、零配置 |
| 官方 `mcp` Python SDK | 协议实现与生态一致，MiniMax 侧可直接挂载 |
| seekdb 走 MySQL 协议 + `pymysql` | 题面禁止用其他数据库；不引 ORM，直连 socket |
| PowerContext 走官方 Python Client | 题面要求提供记忆与检索能力，复用官方实现 |
| MiniMax 走 OpenAI 兼容 `/chat/completions` | 换模型只改 `MINIMAX_MODEL`，客户端零依赖厂商 SDK |
| 模型输出强制 JSON + 容错解析 | 「自动生成修复建议」需要结构化、可直接执行 |
| 写操作默认关闭 | 模型误改生产库的风险，用开关而非提示词兜底 |

## 实测结果

| 指标 | 结果 |
|---|---|
| MCP 工具数 | 7（结构/查询/指标/建议/应用/验证/检索） |
| 端到端验证 | `verify/run_verification.sh` 退出码 0（结构 → 指标 → 建议 → 应用 → 验证 → 检索全通） |
| 修复行为一致性 | 修复前后 `orders` 行数一致（20000 → 20000），只多出修复索引 |
| 模型调用留痕 | `verify/logs/minimax_calls.jsonl`：每次调用含完整 system/user prompt、请求体、原始响应、耗时 |
| 状态日志 | `verify/logs/status.log`：每个工具的 start/ok/failed 流转 |
| MCP 协议联通 | 以真实 MCP 客户端完成 initialize → list_tools → call_tool |

## 边界

- `apply_fix` 只执行模型给出的 SQL，不做语义校验；默认关闭写操作（`MINIMAX_MCP_ALLOW_WRITES=0`）。
- 工具层保证的是「结构化、可执行、可验证」，不保证建议最优。
- `powercontext_search` 需要可连接的 PowerContext 实例，未启动时其余工具不受影响。
