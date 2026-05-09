# CTF Daemon · 精灵进程

> *"Daemon" — a Unix background process, and a spirit that works while you sleep.*  
> *精灵进程：你只管睡觉，它替你解题。*

Worker-pattern autonomous CTF solver. The **精灵 (spirit)** wakes every 60 seconds,
feeds challenges to the AI agent, collects flags, submits them — all without supervision.

```
                      ┌─────────────────────┐
                      │     CTF Daemon       │
                      │    (精灵进程)         │
                      │                      │
  GZCTF Platform ────▶│  wakes every 60s     │────▶ /tmp/ctf_task.json
                      │  picks next challenge │
                      │  creates containers   │
                      │  submits flags        │────▶ /tmp/ctf_abort
                      │  signals abort        │
                      │  remembers history    │
                      └─────────────────────┘
                               ▲
                               │ /tmp/ctf_flag.txt
                               │
                      ┌─────────────────────┐
                      │    Hermes Agent      │
                      │    (AI Solver)       │
                      │                      │
                      │  241 Kali MCP tools  │
                      │  nmap · sqlmap       │
                      │  nuclei · pwnpasi    │
                      └─────────────────────┘
```

## 精灵的能力 · What the Daemon Does

| 能力 | 说明 |
|------|------|
| 🔄 **轮询守护** | 每 60s 醒来一次，检查进度、提交 flag、分配新题 |
| ⏰ **超时放弃** | 600s 解不出？自动放弃，换下一题，不堵路 |
| 🛑 **中断信号** | 写 `/tmp/ctf_abort`，告诉 agent "别白费力气了，换题" |
| 📝 **记忆回溯** | 记录每次尝试用了什么工具、什么结果，重试时注入上下文 |
| 🔙 **退避重试** | 失败后 60s→120s→240s→480s→1800s 逐级冷却，不反复撞墙 |
| 🚫 **永久放弃** | 4 次还解不出？标记为永久跳过，不浪费生命 |
| 🌐 **基础设施容错** | 网络抖动、API 502、LLM 限流 → 不扣重试次数 |
| 📦 **容器自管理** | 自动创建 DynamicContainer，等 60s 就绪，拿实例入口 |
| 🚩 **Flag 嗅探** | 扫描题目描述、附件内容、文件字符串，发现 flag 直接提交 |
| 💾 **救援缓存** | 提交失败的 flag 存到 `/tmp/ctf_flags_rescue.txt`，下次重试 |

## 快速开始 · Quick Start

### 1. 召唤精灵

```bash
git clone https://github.com/chaojixinren/ctf-autosolver.git
cd ctf-autosolver
pip install requests
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
CTF_TASK_TIMEOUT_SECONDS=600      # 最多等多久（秒）
CTF_MAX_RETRIES_PER_CHALLENGE=4   # 最多重试几次
CTF_BASE_RETRY_COOLDOWN=60        # 基础冷却时间（秒）
CTF_MAX_RETRY_COOLDOWN=1800       # 最长冷却时间（秒）
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
  1. execute_code → python3 ctf_daemon.py
     精灵的回应（机器可读）：
     "TASK:id:category:title"   → 有新任务！
     "ABORT:id:reason:elapsed"  → 精灵放弃了当前题，你也停手！
     "PENDING:id:elapsed_secs"  → 还在工作中，继续
     "DONE"                     → 全部完成，功成身退

  2. 收到 ABORT → 检查 /tmp/ctf_abort，立刻停手，回到 1

  3. 收到 TASK → read_file /tmp/ctf_task.json
     → retry_number: 第几次尝试
     → attempt_history: 精灵记录的前几次尝试（别再踩同样的坑！）

  4. 用 Kali MCP 工具解题（nmap, sqlmap, nuclei, gobuster, pwnpasi...）
     过程中每隔 5-10 个工具调用检查一次 /tmp/ctf_abort

  5. 找到 flag → write_file /tmp/ctf_flag.txt（只要 flag，不要废话）

  6. 回到 1
```

## 精灵的成长史 · Version History

| 版本 | 新能力 |
|------|--------|
| **v2.1** 🆕 | 中断信号（/tmp/ctf_abort）、尝试记忆（attempt_history）、机器可读输出 |
| **v2.0** | 超时放弃、退避冷却、最大重试、基础设施容错 |
| **v1.0** | 基础精灵模式：轮询 - 出题 - 收 flag |

## 灵感来源 · Inspiration

借鉴了 [LingXi（灵犀）](https://github.com/chaojixinren/LingXi) 的并发任务模型——它教会了精灵"一道题卡住了，先去解别的"。

## 精灵的档案 · Files

| 文件 | 用途 |
|------|------|
| `ctf_daemon.py` | **精灵本体**。60s 一次循环，管理一切。 |
| `solver.py` | 一次性求解器 + 状态管理。 |
| `gzctf_client.py` | GZCTF REST API —— 登录、拉题、提交。 |
| `challenge_engine.py` | 题目分析、flag 提取、策略判断。 |
| `submit_flag.py` | 命令行手动提交 flag。 |
| `config.env.example` | 契约模板（复制为 config.env 并填入凭据）。 |

## 许可 · License

MIT — 召唤精灵，赢得比赛。
