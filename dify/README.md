# Dify 赛道交付说明

> 赛题：Dify（20 分）｜作品：**灯塔守夜人** —— 一个会记住玩家恶作剧的 NPC

## 1. 作品创意

雾夜里守灯塔的 NPC **阿澜**。玩家说什么、做什么，她不会责怪，但会**记下来**；
下一次玩家再来，她会提起上一次的「壮举」。

玩法很简单：玩家在对话框里说一句与灯塔有关的话（关灯、藏渔网、唱跑调的船歌），
NPC 按自己的性格回应，并在后续轮次里**引用玩家之前做过的事**。

- **Dify** 负责核心流程编排：接收输入 → 调用配套服务取上下文 → LLM 按人设作答 → 输出。
- **seekdb** 负责**世界设定**：每个 NPC 的人设、秘密、说话风格都存在 `npc_setting` 表，
  按关键词检索后作为上下文注入提示词。
- **PowerContext** 负责**玩家记忆**：每一轮对话作为 Source 入库，后台处理成 Memory，
  下一轮通过 `POST /v1/memory/search` 召回，让 NPC 真的「记得」。

## 2. 目录

```
dify/
├── README.md                    本文件
├── dsl/npc-memory-chat.yml      Dify 应用 DSL（导入即用）
├── service/app.py               配套后端：设定检索 + 记忆召回/写入
├── service/seed_world.py        把 NPC 设定灌进 seekdb
├── service/test_turn.py         端到端自测（跨会话召回）
└── run_check.sh                 一条命令跑完 seed + 服务 + 自测
```

## 3. 运行说明

### 3.1 依赖与版本

| 组件 | 版本 | 说明 |
|---|---|---|
| Dify | 1.x（DSL version 0.1.5） | Cloud 或自建均可 |
| seekdb | 1.4.0 | 用 MySQL 协议访问，默认 `127.0.0.1:2881`，用户 `root`、空密码 |
| PowerContext | 1.2.x | HTTP API，默认 `http://127.0.0.1:8000` |
| Python | 3.11+ | 配套服务只需要标准库 + `pymysql` |

模型服务：DSL 里的 LLM 节点默认用 `tongyi/qwen-plus`，**请在导入后按你的模型服务改成
实际可用的 provider 与模型名**。配套服务不调用模型，只调用 seekdb 与 PowerContext。

### 3.2 启动顺序

```bash
# 1) seekdb
docker run --name seekdb -p 2881:2881 -d oceanbase/seekdb:1.4.0
# 或 ./seekdb --nodaemon --parameter memory_budget=4G --parameter cpu_count=4

# 2) PowerContext（用 seekdb 或 sqlite 均可，这里用 sqlite 便于起步）
powercontext serve --database sqlite
# 若你的 PowerContext 走环境变量，见其 README 的 POWERCONTEXT_SERVER_* 说明

# 3) 灌入 NPC 设定（seekdb 侧）
python3 service/seed_world.py
#    可用环境变量覆盖连接：NPC_SEEKDB_SOCKET / NPC_SEEKDB_HOST / NPC_SEEKDB_PORT /
#    NPC_SEEKDB_USER / NPC_SEEKDB_PASSWORD / NPC_SEEKDB_DATABASE

# 4) 启动配套服务
python3 service/app.py --port 8002
#    可用环境变量覆盖：NPC_POWERCONTEXT_URL / NPC_POWERCONTEXT_TOKEN

# 5) 导入 DSL
#    Dify 控制台 → 工作室 → 导入 DSL → 选择 dsl/npc-memory-chat.yml
#    导入后把 HTTP Request 节点的 url 改成你的配套服务地址（默认 http://127.0.0.1:8002/npc/turn）

# 6) 跑一遍自测，确认两个后端都活着
bash run_check.sh
```

### 3.3 已知限制

- 配套服务用 `urllib` 直连两个后端，只用标准库 + `pymysql`；若 PowerContext 开了鉴权，
  请设置 `NPC_POWERCONTEXT_TOKEN`。
- DSL 里的 LLM 节点需要你按实际模型服务替换 provider/模型名；应用本身不依赖特定模型。
- 记忆是异步生成的：刚写入的一轮不会立刻出现在召回里，界面上表现为「第一轮不记得、
  下一轮记得」。这是设计如此（写入 → 后台处理 → 召回），验证样例按这个节奏编写。

## 4. 验证样例（可按步骤复现）

### 样例一：NPC 知道自己的设定

- 初始条件：已执行 `seed_world.py`；PowerContext 的 scope 名 `npc-game`。
- 输入（对话框）：`我把灯塔的路灯全关了`；NPC 选择 `阿澜`。
- 操作步骤：
  1. 打开 Dify 应用，输入上面这句话；
  2. 观察回答是否提到「灯」「灯塔」「船」等设定里的元素。
- 预期表现：回答里出现与灯塔/点灯/雾夜相关的描述，且语气克制、先描述环境。
- 证据：`run_check.sh` 的输出里 `setting` 字段来自 seekdb 的 `npc_setting` 表。

### 样例二：跨会话记忆被召回（核心技术结合的验证）

- 初始条件：PowerContext 可用；scope `npc-game-test` 已创建。
- 输入与顺序：
  1. 第一轮：`我把灯塔的路灯全关了`
  2. 等待后台处理完成（约一个处理周期）
  3. 第二轮：`灯塔的灯还亮着吗`
- 预期表现：第二轮的回答里提到玩家**之前关过灯**这件事。
- 证据：`run_check.sh` 的输出中 `recalled` 字段包含
  `玩家 tester 对 NPC 阿澜 说：我把灯塔的路灯全关了`；
  PowerContext 侧可用 `POST /v1/memory/entries/list {"scope_id":"<scope id>"}` 复核条目。

### 样例三：结果不足时如实说明

- 输入：选一个没有登记设定的 NPC（例如 `不存在的人`），说一句 `你好`。
- 预期表现：回答表示没有该 NPC 的设定，而不是编造人设。

### 样例四：还原初始状态

```bash
# 清掉演示用 scope 的记忆
curl -s -X POST http://127.0.0.1:8000/v1/scopes/<scope id>/sources/<source_type>/<source_id> ...
# 或直接删掉 PowerContext 的本地数据库后重启，最省事
rm -f ~/.local/share/powercontext/powercontext.db*
```

## 5. 配置示例（占位值，不要提交真实凭据）

```bash
export NPC_SEEKDB_SOCKET=/home/hsx/seekdb/run/sql.sock
export NPC_SEEKDB_DATABASE=npc_world
export NPC_POWERCONTEXT_URL=http://127.0.0.1:8000
export NPC_POWERCONTEXT_TOKEN=<your token>
```
