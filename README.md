# CTF 精灵 · Daemon v3.4.4

> *"Daemon" — a Unix background process, and a spirit that works while you sleep.*  
> *你只管睡觉，精灵替你解题。精灵学会了分身术+搬家术，每道题有独立的家。*

Worker-pattern autonomous CTF solver. The **精灵 (spirit)** wakes every 60 seconds,
fills up to 3 concurrent challenge slots, collects flags from all, submits them — 
all without supervision. **v3.4 adds per-challenge isolated workdirs** — no more
file confusion when 19 challenges dump attachments into one folder.
**v3.4.4 fixes pure-Chinese title collision** — challenges with titles that sanitize to the same string (e.g. all-Chinese names) now get unique workdirs via `_{id}` suffix.

```
                      ┌──────────────────────────────────────┐
                      │       CTF Daemon v3.4                 │
                      │      (精灵 · 分身 + 搬家)               │
                      │                                       │
  GZCTF Platform ────▶│  Slot 0: Misc challenge    ───▶ ...  │
                      │    └─ workdir: /tmp/ctf_cyberchef/    │
                      │  Slot 1: Crypto challenge  ───▶ ...  │
                      │    └─ workdir: /tmp/ctf_ez_dsa/       │
                      │  Slot 2: Pwn challenge     ───▶ ...  │
                      │    └─ workdir: /tmp/ctf_babyrop/      │
                      │                                       │
                      │  each slot: isolated workdir          │
                      │  each slot: timeout / abort            │
                      │  each slot: independent retry          │
                      │  one stuck ≠ all stuck                 │
                      └──────────────────────────────────────┘
                               ▲         ▲         ▲
                               │ flags   │ flags   │ flags
                      ┌────────┴─────────┴─────────┴──────────┐
                      │        Hermes Agent                    │
                      │        (AI Solver)                     │
                      │  241 Kali MCP tools                    │
                      │  reads slot_N.json → finds workdir     │
                      │  all solving inside workdir            │
                      └───────────────────────────────────────┘
```

## 推荐部署方案 · Recommended Stack

精灵需要四样东西才能施展全力：

```
┌─────────────────────────────────────────────────────┐
│                   Kali Linux                        │
│  ┌───────────────────────────────────────────────┐ │
│  │          Kali-Security-MCP (200+ tools)        │ │
│  │  github.com/SeaC-25/Kali-Security-MCP          │ │
│  │  nmap · sqlmap · nuclei · hydra · msf          │ │
│  │  gobuster · pwnpasi · hashcat · john ...       │ │
│  └──────────────────┬────────────────────────────┘ │
│                     │ MCP Protocol                  │
│  ┌──────────────────▼────────────────────────────┐ │
│  │          Hermes Agent (AI brain)               │ │
│  │  nousresearch.com/hermes-agent                 │ │
│  └──────────────────┬────────────────────────────┘ │
│                     │                              │
│  ┌──────────────────▼────────────────────────────┐ │
│  │          CTF 精灵 · Daemon (orchestrator)       │ │
│  │  github.com/chaojixinren/ctf-daemon             │ │
│  │  轮询 · 调度 · 超时 · 记忆 · 提交 · 隔离        │ │
│  └──────────────────┬────────────────────────────┘ │
└─────────────────────┼──────────────────────────────┘
                      │
                      ▼
              ┌──────────────┐
              │  GZCTF 平台   │
              └──────────────┘
```

| 组件 | 项目 | 作用 |
|------|------|------|
| 🐉 **Kali Linux** | 操作系统 | 200+ 安全工具的运行环境 |
| 🔧 **Kali-Security-MCP** | [SeaC-25/Kali-Security-MCP](https://github.com/SeaC-25/Kali-Security-MCP) | 通过 MCP 协议将 Kali 工具暴露给 AI |
| 🧠 **Hermes Agent** | [nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent) | AI 大脑，调用 MCP 工具解题 |
| 👻 **CTF 精灵** | 本项目 | 调度中枢，管理一切流程 |

### 部署步骤

```bash
# 1. 安装 Kali-Security-MCP（武器库）
git clone https://github.com/SeaC-25/Kali-Security-MCP.git ~/Kali-Security-MCP
cd ~/Kali-Security-MCP
pip install -r requirements.txt --break-system-packages
python status_check.py

# 2. 配置 Hermes（大脑）
# 在 Hermes 的 config.yaml 中添加 Kali MCP server
hermes config set mcp_servers.kali.command "python"
hermes config set mcp_servers.kali.args '["/home/kali/Kali-Security-MCP/mcp_server.py"]'

# 3. 召唤精灵（调度中枢）
git clone https://github.com/chaojixinren/ctf-daemon.git ~/ctf-daemon
cd ~/ctf-daemon
cp config.env.example config.env
# 编辑 config.env 填入 GZCTF 凭据

# 4. 唤醒精灵
hermes cronjob create \
  --name "CTF 精灵" \
  --schedule "every 1m" \
  --script ctf_daemon.py \
  --no-agent
```

## 精灵的能力 · What the Daemon Does

| 能力 | 说明 |
|------|------|
| 📁 **目录隔离 (v3.4)** | 每道题拥有独立工作目录 `/tmp/ctf_{题目名}/`，附件/脚本/产物互不干扰 |
| 🪞 **并发分身 (v3)** | 最多 3 个槽位同时跑不同题目，一个卡住不影响其他 |
| 🔄 **轮询守护** | 每 60s 醒来一次，检查进度、提交 flag、分配新题 |
| ⏰ **超时放弃** | 600s 解不出？自动放弃，换下一题，不堵路 |
| 🛑 **中断信号** | 写 `/tmp/ctf_tasks/abort_N`，告诉 agent "别白费力气了" |
| 📝 **记忆回溯** | 记录每次尝试用了什么工具、什么结果，重试时注入上下文 |
| 🔙 **退避重试** | 失败后 60s→120s→240s→480s→1800s 逐级冷却，永不放弃 |
| 🌐 **基础设施容错** | 网络抖动、API 502、LLM 限流 → 不扣重试次数 |
| 📦 **容器全生命周期 (v3.4)** | 自动创建/续期/健康检查/删除；死容器 6s 检测，资源 0 残留 |
| 🚩 **Flag 嗅探** | 扫描题目描述、附件内容、文件字符串 |
| 🧠 **LLM 驱动选择** | Daemon 输出菜单 → LLM 分析难度 → 写回优先级排序 |
| ✅ **平台交叉验证** | 提交后对比平台 ground truth，不盲目信任本地状态 |

## 快速开始 · Quick Start

### 1. 召唤精灵

```bash
git clone https://github.com/chaojixinren/ctf-daemon.git
cd ctf-daemon
pip install requests python-dotenv
```

### 2. 签订契约（配置）

```bash
cp config.env.example config.env
```

```ini
GZCTF_BASE_URL=http://your-gzctf-server:8080
GZCTF_USERNAME=your_username
GZCTF_PASSWORD=your_password
GZCTF_GAME_ID=0          # 0 = 自动发现比赛
GZCTF_TEAM_NAME=AI_Solver

# 精灵的脾气（可按需调整）
CTF_TASK_TIMEOUT_SECONDS=600      # 单槽超时（秒）
CTF_MAX_RETRIES_PER_CHALLENGE=4   # 最多重试几次
CTF_BASE_RETRY_COOLDOWN=60        # 基础冷却（秒）
CTF_MAX_RETRY_COOLDOWN=1800       # 最长冷却（秒）
CTF_CONCURRENT_SLOTS=3            # 并发槽位数（1=v2兼容模式）
CTF_WORKDIR_BASE=/tmp             # 题目工作目录基路径
```

### 3. 唤醒精灵

```bash
# 方式一：传统 cron
(crontab -l 2>/dev/null; echo "* * * * * cd $(pwd) && python3 ctf_daemon.py >> /tmp/ctf_daemon.log 2>&1") | crontab -

# 方式二：Hermes 契约
hermes cronjob create \
  --name "CTF Daemon Safety Net" \
  --schedule "every 1m" \
  --script ctf_daemon.py \
  --no-agent
```

### 4. 手动驱使

```bash
python3 ctf_daemon.py                # 唤醒一次（写任务 / 收 flag）
python3 solver.py status             # 查看精灵的状态
python3 solver.py list               # 列出所有题目
python3 submit_flag.py <id> "flag{...}"  # 手动献祭 flag
```

## Agent 如何与精灵协作 · The Loop

```
LOOP:
  1. 运行 daemon: cd /path/to/ctf-daemon && python3 ctf_daemon.py
     解析输出：
     "MENU:N:..."         → Daemon 写了 /tmp/ctf_menu.jsonl，等待 LLM 选择
                             第 2 步：LLM 读取菜单，分析难度，写回 selection
     "DISPATCH:2/3:..."   → 槽位已填充。第 3 步：解题
     "BUSY:3/3:..."       → 所有槽位占用中。第 3 步：解题
     "WAITING:N..."       → 题目在冷却中。等待 60s，回到 1
     "DONE"               → 全部题目平台验证通过。结束

  2. LLM SELECTION（daemon 输出 MENU 时）：
     a. read_file /tmp/ctf_menu.jsonl — 分析每题：
        - DynamicAttachment 优先于 DynamicContainer（无需等容器）
        - 检查 content_preview 中是否已有 flag
        - Misc/Crypto 通常比 Web/Pwn 容易
     b. 选 3 道最有把握的题
     c. write_file /tmp/ctf_selection.json:
        {"challenge_ids": ["id1","id2","id3"], "reasoning": "..."}
     d. 回到 1（daemon 会读取 selection 并分发）

  3. SOLVE 已分发的槽位：
     a. read_file /tmp/ctf_tasks/slot_N.json
     b. 先检查 /tmp/ctf_tasks/abort_N — 如果存在，跳过此槽位
     c. 查看 "workdir" 字段 → 所有解题都在该目录下进行
        附件在 workdir 里，脚本写 workdir 里，中间文件放 workdir 里
     d. 用 Kali MCP 工具解题
     e. attempt_history 字段显示之前试过的工具 — 别重蹈覆辙
     f. 找到 flag → write_file /tmp/ctf_tasks/flag_N.txt
        内容只写 flag 字符串，例：dutctf{some_flag_here}

  4. 回到 1
```

## 目录隔离架构 · Workdir Isolation (v3.4)

```
/tmp/ctf_ez_dsa/              ← Ez_DSA 题目的家
├── att.py                     ← 题目附件
├── solve.py                   ← Agent 写的解题脚本
└── output.txt                 ← 工具输出

/tmp/ctf_cyberchef_recipe/    ← cyberchef_recipe 题目的家
├── recipe                     ← 题目附件
├── cyberchef_recipe.zip
├── extract.py                 ← 解题脚本
└── recipe_reversed.json

/tmp/ctf_babyrop/              ← babyrop 题目的家
├── babyrop                    ← 二进制附件
├── exploit.py                 ← 利用脚本
└── leak_output.bin

/tmp/ctf_tasks/                ← 槽位文件（不变）
├── slot_0.json                ← 含 "workdir" 字段指向对应目录
├── slot_1.json
├── slot_2.json
├── flag_0.txt                 ← Agent 写 flag 的地方
├── flag_1.txt
└── abort_0
```

**命名规则**：题目名 → 小写 + 特殊字符转下划线 + `_{id}` 后缀保证唯一
| 题目名 | 目录名 |
|--------|--------|
| `Ez_DSA` (ID:4) | `/tmp/ctf_ez_dsa_4/` |
| `BY_Caesar` (ID:2) | `/tmp/ctf_by_caesar_2/` |
| `CGI` (ID:15) | `/tmp/ctf_cgi_15/` |
| `writeup_channel` (ID:20) | `/tmp/ctf_writeup_channel_20/` |
| `!!! test !!!` (ID:99) | `/tmp/ctf_test_99/` |

## 精灵的成长史 · Version History

| 版本 | 新能力 |
|------|--------|
| **v3.4.4** | 🐛 纯中文标题碰撞修复：sanitize 后同名的题目通过 `_{id}` 后缀获得独立工作目录，不再冲突 |
| **v3.3** | 平台交叉验证、永不放弃（移除 permanent_failed）、ID 漂移检测 |
| **v3.2** | LLM 驱动选择：输出 MENU → LLM 分析难度 → 写回优先顺序 |
| **v3.1** | 并发分身：多槽位同时运行，`CTF_CONCURRENT_SLOTS=3` |
| **v2.1** | 中断信号（/tmp/ctf_tasks/abort_N）、尝试记忆（attempt_history） |
| **v2.0** | 超时放弃、退避冷却、最大重试、基础设施容错 |
| **v1.0** | 基础精灵模式：单槽轮询 - 出题 - 收 flag |

### v3.4 vs v3.3: What Changed

| | v3.3 | v3.4 |
|---|------|------|
| **附件目录** | 全部混在 `/tmp/ctf_attachments/` | 每题独立 `/tmp/ctf_{题目名}/` |
| **工作目录** | Agent 自己决定（容易乱） | Daemon 创建，slot 文件明确指定 |
| **容器管理** | 只创建不删除，残留在平台 | 创建/续期/探活/删除全闭环 |
| **配置** | `ATTACHMENT_DIR` | `CTF_WORKDIR_BASE` |
| **文件混乱** | 19 道题的附件同名冲突 | 完全隔离，互不干扰 |

## 灵感来源 · Inspiration

- 并发任务模型借鉴了 [**LingXi（灵犀）**](https://github.com/chaojixinren/LingXi) —— "一道题卡住了，先去解别的"
- GZCTF API 交互模式借鉴了 [**Misuzu**](https://github.com/TechnickOcean/Misuzu) 的 GZCTF 插件 —— 多 Agent 并发 CTF 系统
- 工具链由 [**Kali-Security-MCP**](https://github.com/SeaC-25/Kali-Security-MCP) 提供 —— 200+ Kali 工具通过 MCP 协议暴露给 AI

## 精灵的档案 · Files

| 文件 | 用途 |
|------|------|
| `ctf_daemon.py` | **精灵本体 v3.4**。60s 循环，多槽位并发，LLM 选择，目录隔离，容器全生命周期。 |
| `state.py` | 持久化状态管理（solved/retries/slots/history）+ `WORKDIR_BASE` 配置。 |
| `solver.py` | 一次性求解器 + flag 提交工具函数。 |
| `gzctf_client.py` | GZCTF REST API —— 登录、拉题、提交。 |
| `challenge_engine.py` | 题目分析、flag 提取、策略判断。 |
| `submit_flag.py` | 命令行手动提交 flag。 |
| `config.env.example` | 契约模板（复制为 config.env 并填入凭据）。 |
| `tests/` | 测试套件（57 个测试用例）。 |
| `/tmp/ctf_tasks/` | v3 任务目录：slot_N.json / flag_N.txt / abort_N |
| `/tmp/ctf_{题目名}/` | v3.4 题目隔离工作目录 |

## 许可 · License

MIT — 召唤精灵，赢得比赛。
