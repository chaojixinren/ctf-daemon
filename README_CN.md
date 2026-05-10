# CTF Daemon · Hermes 原生全自动竞赛框架

[English](README.md)

> *你睡觉。守护进程在干活。*

一个 Hermes Agent 原生的全自动 CTF 解题框架。支持多槽位并发调度、单题独立工作目录、
容器全生命周期管理、LLM 驱动的题目选择、平台验证的提交管线。设计目标：在整场比赛期间无
人值守运行——从开场到积分榜冻结。

**运行时**：Hermes Agent + Kali-Security-MCP + GZCTF（或兼容平台）

```text
┌──────────────────────────────────────────────────────────────────┐
│                        HERMES AGENT                               │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │              CTF DAEMON（调度核心）                           │  │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐                    │  │
│  │  │ Slot 0  │  │ Slot 1  │  │ Slot 2  │  … 最多 N 个槽位   │  │
│  │  │ 工作目录│  │ 工作目录│  │ 工作目录│    各槽位独立       │  │
│  │  │ 超时    │  │ 超时    │  │ 超时    │    重试互不干扰     │  │
│  │  └────┬────┘  └────┬────┘  └────┬────┘                    │  │
│  │       │            │            │                           │  │
│  │       ▼            ▼            ▼                           │  │
│  │  ┌─────────────────────────────────────────────────────┐   │  │
│  │  │              Kali-Security-MCP（241 个工具）         │   │  │
│  │  │  nmap · sqlmap · nuclei · hydra · msf · pwnpasi     │   │  │
│  │  │  gobuster · ffuf · hashcat · john · radare2 …       │   │  │
│  │  └─────────────────────────────────────────────────────┘   │  │
│  └────────────────────────────────────────────────────────────┘  │
│       │                                                          │
│       ▼                                                          │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                   GZCTF 平台                                 │  │
│  │  登录 · 题目获取 · 容器管理 · flag 提交                       │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

## 技术栈

| 组件 | 角色 | 仓库 |
|-----------|------|------------|
| **Hermes Agent** | AI 推理引擎——读取槽位任务、调用工具、写出 flag | [nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent) |
| **Kali-Security-MCP** | 武器库——241 个 Kali 工具通过 MCP 协议暴露 | [SeaC-25/Kali-Security-MCP](https://github.com/SeaC-25/Kali-Security-MCP) |
| **CTF Daemon** | 调度核心——分发、重试、冷却、容器生命周期、平台同步 | 本仓库 |

## 核心能力

| 能力 | 详情 |
|------------|--------|
| **并发调度** | 最多 `CTF_CONCURRENT_SLOTS` 道题并行求解。一个槽位卡住不会阻塞其他槽位。 |
| **工作目录隔离** | 每道题拥有独立的 `/tmp/ctf_{title}_{id}/`——附件、脚本、产物均限定在题目范围内。40+ 道题之间零文件冲突。 |
| **容器生命周期** | 自动创建、续期（超过 30 分钟时）、健康检查（TCP 连接 + HTTP 存活探测）、释放时删除。死容器约 9 秒检出，不计入重试次数。 |
| **LLM 驱动选题** | 守护进程输出题目列表 → Hermes LLM 读取、分析难度、写出优先级排序 → 守护进程按优先级调度。无需硬编码策略。 |
| **递增退避** | 失败题目冷却时间递增：60s → 120s → 240s → … → 1800s。基础设施故障（API 502、容器死亡、频率限制）不计入重试次数。 |
| **平台验证提交** | `submit_and_record()` 轮询状态端点。仅在返回 `Accepted` / `AlreadySolved` 时标记为已解。交叉核验队伍提交接口——从不单方面信任本地状态。 |
| **跨题防护** | flag 文件为 JSON 格式，包含 `challenge_id`。守护进程在提交前校验当前槽位的 `ch_id` 是否匹配。防止交错题目提交 flag。 |
| **会话恢复** | Cookie 过期自动重新登录。API 请求支持指数退避重试（3 次）。菜单/选择文件采用原子写入，避免 cron 竞态。 |
| **尝试历史** | 记录每道题使用的工具、摘要、错误信息。注入重试上下文，让 LLM 从之前的失败中学习。 |

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/chaojixinren/ctf-daemon.git
cd ctf-daemon
pip install requests

# 2. 配置
cp config.env.example config.env
# 编辑：GZCTF_BASE_URL, GZCTF_USERNAME, GZCTF_PASSWORD

# 3. 单次运行（手动）
python3 ctf_daemon.py

# 4. 部署为 Hermes cron（全自动）
hermes cronjob create \
  --name "CTF Daemon" \
  --schedule "every 1m" \
  --prompt "Run the CTF daemon: cd /path/to/ctf-daemon && python3 ctf_daemon.py. If MENU: read menu, pick 3 best challenges, write selection. Re-run. Solve dispatched slots. Write flags in JSON: {\"challenge_id\": N, \"flag\": \"...\"}"
```

## 配置说明

```ini
# 平台凭据
GZCTF_BASE_URL=http://your-gzctf-server:8080
GZCTF_USERNAME=your_username
GZCTF_PASSWORD=your_password
GZCTF_GAME_ID=0              # 0 = 自动发现
GZCTF_TEAM_NAME=AI_Solver

# 守护进程调优
CTF_CONCURRENT_SLOTS=3        # 并行解题槽位数
CTF_TASK_TIMEOUT_SECONDS=600  # 单槽位超时时间（秒）
CTF_MAX_RETRIES_PER_CHALLENGE=4
CTF_BASE_RETRY_COOLDOWN=60    # 基础退避时间（秒）
CTF_MAX_RETRY_COOLDOWN=1800   # 退避上限（秒）
CTF_WORKDIR_BASE=/tmp         # 单题工作目录根路径
```

## Agent 循环

Hermes agent 执行以下循环，直到守护进程输出 `DONE`（所有题目均经平台验证已解）：

```text
循环:
  1. 运行守护进程: python3 ctf_daemon.py
     解析输出行:
     "MENU:N:..."        → 守护进程已写入 /tmp/ctf_menu.jsonl。转到步骤 2。
     "DISPATCH:N/3:..."   → 槽位已填满。转到步骤 3。
     "BUSY:3/3:..."       → 所有槽位忙碌。转到步骤 3。
     "WAITING:N..."       → 题目处于冷却期。等待 60s。转到步骤 1。
     "DONE"               → 所有题目已在平台验证已解。停止。

  2. LLM 选题:
     a. read_file /tmp/ctf_menu.jsonl
     b. 分析：DynamicAttachment > DynamicContainer，检查 content_preview 中是否有 flag
     c. 挑选 3 道最有希望的题目
     d. write_file /tmp/ctf_selection.json:
        {"challenge_ids": ["id1","id2","id3"], "reasoning": "..."}
     e. 转到步骤 1

  3. 求解已分配的槽位:
     a. read_file /tmp/ctf_tasks/slot_N.json
     b. 先检查 /tmp/ctf_tasks/abort_N——如果存在，跳过此槽位
     c. 所有操作在 "workdir" 字段指定的目录中进行
     d. 使用 Kali MCP 工具（mcp_kali_*）求解
     e. attempt_history 显示了之前的失败尝试——避免重复
     f. 找到 flag → write_file /tmp/ctf_tasks/flag_N.txt:
        {"challenge_id": <N>, "flag": "dutctf{...}"}

  4. 转到步骤 1
```

## 文件系统布局

```text
/tmp/ctf_tasks/               # 槽位目录（守护进程管理）
├── slot_0.json               # 题目任务，含工作目录、工具、历史记录
├── slot_1.json
├── flag_0.txt                # Agent 写入 JSON: {"challenge_id":N, "flag":"..."}
├── flag_1.txt
├── abort_0                   # 守护进程写入：停止该槽位的工作
└── abort_1

/tmp/ctf_{title}_{id}/        # 单题独立工作目录
├── <附件文件>                 # 由守护进程下载
├── exploit.py                # Agent 编写的脚本
└── output.txt                # 工具输出

/tmp/ctf_menu.jsonl           # 守护进程 → LLM：可选题列表
/tmp/ctf_selection.json       # LLM → 守护进程：优先级排序

~/.hermes/ctf_state.json      # 持久化状态（已解、重试、历史记录）
```

### 工作目录命名规则

| 题目名称 | 工作目录 |
|-----------------|---------|
| `Hello_World` (ID:1) | `/tmp/ctf_hello_world_1/` |
| `!!! welcome !!!` (ID:3) | `/tmp/ctf_welcome_3/` |
| `签到` (ID:7) | `/tmp/ctf_challenge_7/` |
| `变异凯撒` (ID:9) | `/tmp/ctf_challenge_9/` |

纯中文题目在处理后为空字符串的情况，通过 `_{id}` 后缀做消歧处理。

## 手动操作

```bash
python3 ctf_daemon.py              # 执行一次守护进程周期
python3 solver.py status           # 导出状态文件
python3 solver.py list             # 列出所有题目及其状态
python3 submit_flag.py <id> "flag{...}"  # 手动提交 flag
```

## 模块说明

| 文件 | 用途 |
|------|---------|
| `ctf_daemon.py` | 调度器：登录、获取题目、LLM 选题、分发、提交、容器生命周期、健康检查 |
| `gzctf_client.py` | GZCTF REST API 客户端——认证、题目、容器、提交、重试逻辑 |
| `state.py` | 持久化状态：已解/重试次数/冷却/槽位/历史记录。路径可通过环境变量覆盖。 |
| `challenge_engine.py` | 题目分析、flag 提取、策略判定、文件分类 |
| `solver.py` | 独立单次求解器 + `submit_and_record()` 工具函数 |
| `submit_flag.py` | 命令行手动提交 flag |

## 版本历史

| 版本 | 变更 |
|---------|---------|
| **v3.5.1** | JSON 格式 flag 文件，带 `challenge_id` 校验。容器 HTTP 存活探测。基于 `/submissions` 接口的按队平台同步。会话自动续登。裸端口 `instanceEntry` 修复。容器错误结构化处理。菜单/选择文件原子写入。API 指数退避重试。增强 flag 过滤。N+1 查询优化。 |
| **v3.4.4** | 通过 `_{id}` 后缀修复纯中文标题工作目录冲突问题 |
| **v3.4.3** | 完整容器生命周期：创建/续期/健康检查/删除 |
| **v3.4** | 单题独立工作目录 |
| **v3.3** | 平台交叉验证。移除永久失败机制——始终可重试。 |
| **v3.2** | 通过菜单/选择协议实现 LLM 驱动选题 |
| **v3.1** | 多槽位并发。代码质量修复。测试套件。 |
| **v2.1** | 中止信号 + 尝试历史 |
| **v2.0** | 超时、退避冷却、最大重试次数、基础设施容错 |

## 致谢

- 并发模型参考 [LingXi（灵犀）](https://github.com/chaojixinren/LingXi)
- GZCTF API 交互模式来自 [Misuzu](https://github.com/TechnickOcean/Misuzu)
- 工具后端：[Kali-Security-MCP](https://github.com/SeaC-25/Kali-Security-MCP) by SeaC-25
- AI 运行时：[Hermes Agent](https://github.com/nousresearch/hermes-agent) by Nous Research

## 许可证

MIT
