# CTF Autosolver — Autonomous GZCTF Competition Solver

> Worker-pattern CTF solver: daemon manages state, Hermes AI Agent does the solving.  
> **No orchestration burden on the agent** — just read task → solve → write flag.

## Architecture

```
┌──────────────┐    /tmp/ctf_task.json    ┌──────────────┐
│  CTF Daemon  │ ──────────────────────▶  │  Hermes Agent │
│  (cron 1min) │ ◀──────────────────────  │  (AI Solver)  │
└──────┬───────┘    /tmp/ctf_flag.txt     └──────────────┘
       │
       ▼
┌──────────────┐
│  GZCTF API   │
│  (auth/fetch/│
│   submit)    │
└──────────────┘
```

- **Daemon** (`ctf_daemon.py`): Runs every 60s via cron. Handles login, challenge discovery, container creation, flag submission. Writes task files for the agent.
- **Agent** (Hermes + Kali MCP): Reads `/tmp/ctf_task.json`, solves the challenge using 241 Kali security tools, writes flag to `/tmp/ctf_flag.txt`.
- **State**: Persistent JSON at `~/.hermes/ctf_state.json` tracks solved, retries, cooldowns.

## Key Features (v2)

| Feature | Description |
|---------|-------------|
| **Non-blocking** | Timeout per task (default 600s). Stuck challenge → skip to next. |
| **Retry + Cooldown** | Escalating backoff: 60s → 120s → 240s → 480s → Max 1800s. |
| **Max Retries** | After 4 attempts, permanently skip the challenge. |
| **Infra Failure Detection** | Network/API/LLM errors don't consume retry count. |
| **DynamicContainer Support** | Auto-creates containers, waits 60s for readiness, re-fetches instance entry. |
| **Auto Flag Detection** | Scans descriptions, attachments, and file strings for flags. |
| **Rescue Cache** | Failed submissions cached to `/tmp/ctf_flags_rescue.txt` for retry. |

## Quick Start

### 1. Prerequisites

- Python 3.10+
- Hermes Agent with Kali MCP Server
- Access to a GZCTF competition platform

### 2. Install

```bash
git clone https://github.com/chaojixinren/ctf-autosolver.git
cd ctf-autosolver
pip install requests
```

### 3. Configure

Copy and edit the config:

```bash
cp config.env.example config.env
```

Edit `config.env`:

```ini
GZCTF_BASE_URL=http://your-gzctf-server:8080
GZCTF_USERNAME=your_username
GZCTF_PASSWORD=your_password
GZCTF_GAME_ID=0          # 0 = auto-discover first available game
GZCTF_TEAM_NAME=AI_Solver
ATTACHMENT_DIR=/tmp/ctf_attachments

# Daemon v2 controls
CTF_TASK_TIMEOUT_SECONDS=600      # Abandon task after 10 min
CTF_MAX_RETRIES_PER_CHALLENGE=4   # Max retries before permanent skip
CTF_BASE_RETRY_COOLDOWN=60        # Base cooldown (escalates 2x per retry)
CTF_MAX_RETRY_COOLDOWN=1800       # Max cooldown ceiling (30 min)
```

### 4. Deploy with Cron

```bash
# Run daemon every 60 seconds
(crontab -l 2>/dev/null; echo "* * * * * cd $(pwd) && python3 ctf_daemon.py >> /tmp/ctf_daemon.log 2>&1") | crontab -

# Or via Hermes cronjob
hermes cronjob create \
  --name "CTF Daemon Safety Net" \
  --schedule "every 1m" \
  --script ctf_daemon.py \
  --no-agent
```

### 5. Manual Usage

```bash
# Run daemon once (writes task file if agent is idle)
python3 ctf_daemon.py

# Check solver status
python3 solver.py status

# List all challenges
python3 solver.py list

# Submit a flag manually
python3 submit_flag.py <challenge_id> "flag{...}"
```

## How the Agent Solves

When the daemon writes `/tmp/ctf_task.json`, the Hermes Agent:

1. Reads the task file (challenge ID, category, strategy, tools, container entry)
2. Loads the `ctf-autosolver` skill for context
3. Uses Kali MCP tools (`nmap`, `sqlmap`, `nuclei`, `gobuster`, `pwnpasi`, etc.)
4. Writes discovered flags to `/tmp/ctf_flag.txt`

The daemon picks up the flag on its next tick and submits it via the GZCTF API.

## Daemon v2 vs v1: What Changed

| | v1 | v2 |
|---|---|---|
| **Blocking** | One task stuck → everything blocked | Timeout → skip → next challenge |
| **Retry** | None | Escalating backoff with cooldown |
| **Max Retries** | None | 4 permanent skip |
| **Infra Failure** | Counts as real failure | Detected, retry count unchanged |
| **State** | `{solved, attempted, failed}` | `+ {retry_counts, cooldown_until, permanently_failed, task_assigned_at}` |

Inspired by [LingXi](https://github.com/chaojixinren/LingXi)'s concurrent task model.

## Files

| File | Purpose |
|------|---------|
| `ctf_daemon.py` | Background orchestrator. Called by cron every 60s. |
| `solver.py` | One-shot solver entry. Can also be run manually. |
| `gzctf_client.py` | GZCTF REST API client (login, fetch, submit). |
| `challenge_engine.py` | Challenge analysis, flag extraction, strategy detection. |
| `submit_flag.py` | CLI for manual flag submission. |
| `config.env.example` | Configuration template. |

## License

MIT — use it, modify it, win CTFs with it.
