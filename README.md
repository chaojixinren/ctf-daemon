# CTF Autosolver v2.1 — Autonomous GZCTF Competition Solver

> Worker-pattern CTF solver: daemon manages state, Hermes AI Agent does the solving.  
> **No orchestration burden on the agent** — just read task → solve → write flag.

## Architecture

```
┌──────────────┐    /tmp/ctf_task.json    ┌──────────────┐
│  CTF Daemon  │ ──────────────────────▶  │  Hermes Agent │
│  (cron 1min) │ ◀──────────────────────  │  (AI Solver)  │
└──────┬───────┘    /tmp/ctf_flag.txt     └──────────────┘
       │            /tmp/ctf_abort  ←─── daemon signals agent to stop (v2.1)
       ▼
┌──────────────┐
│  GZCTF API   │
│  (auth/fetch/│
│   submit)    │
└──────────────┘
```

- **Daemon** (`ctf_daemon.py`): Runs every 60s via cron. Login, challenge discovery, container creation, flag submission, abort signaling, attempt history tracking.
- **Agent** (Hermes + Kali MCP): Reads `/tmp/ctf_task.json`, solves with 241 Kali tools, writes `/tmp/ctf_flag.txt`. Checks `/tmp/ctf_abort` to stop wasted work.
- **State**: `~/.hermes/ctf_state.json` — persistent JSON with retry counts, cooldowns, attempt history.

## Key Features

| Feature | v2.1 | Description |
|---------|------|-------------|
| **Non-blocking** | ✅ | Timeout per task (600s). Stuck → auto-abort → next challenge. |
| **Abort Signal** | ✅ 🆕 | `/tmp/ctf_abort` tells the agent to stop wasted work immediately. |
| **Attempt History** | ✅ 🆕 | Tracks what was tried. Injected into task on retry — agent learns from failures. |
| **Retry + Cooldown** | ✅ | Escalating backoff: 60s→120s→240s→480s→1800s max. |
| **Max Retries** | ✅ | After 4 attempts, permanently skip. |
| **Infra Failure Detection** | ✅ | Network/LLM errors don't consume retry count. |
| **DynamicContainer** | ✅ | Auto-create containers, wait 60s, re-fetch instance entry. |
| **Auto Flag Detection** | ✅ | Scans descriptions, attachments, file strings. |
| **Rescue Cache** | ✅ | Failed submissions saved to `/tmp/ctf_flags_rescue.txt` for retry. |

## Quick Start

### 1. Prerequisites

- Python 3.10+
- Hermes Agent with Kali MCP Server
- GZCTF competition platform access

### 2. Install

```bash
git clone https://github.com/chaojixinren/ctf-autosolver.git
cd ctf-autosolver
pip install requests
```

### 3. Configure

```bash
cp config.env.example config.env
```

Edit `config.env`:

```ini
GZCTF_BASE_URL=http://your-gzctf-server:8080
GZCTF_USERNAME=your_username
GZCTF_PASSWORD=your_password
GZCTF_GAME_ID=0          # 0 = auto-discover
GZCTF_TEAM_NAME=AI_Solver
ATTACHMENT_DIR=/tmp/ctf_attachments

# Daemon v2.1 controls
CTF_TASK_TIMEOUT_SECONDS=600      # Abandon task after 10 min
CTF_MAX_RETRIES_PER_CHALLENGE=4   # Max retries before permanent skip
CTF_BASE_RETRY_COOLDOWN=60        # Base cooldown (escalates 2x per retry)
CTF_MAX_RETRY_COOLDOWN=1800       # Max cooldown ceiling (30 min)
```

### 4. Deploy with Cron

```bash
# Standard cron
(crontab -l 2>/dev/null; echo "* * * * * cd $(pwd) && python3 ctf_daemon.py >> /tmp/ctf_daemon.log 2>&1") | crontab -

# Or via Hermes
hermes cronjob create \
  --name "CTF Daemon Safety Net" \
  --schedule "every 1m" \
  --script ctf_daemon.py \
  --no-agent
```

### 5. Manual Usage

```bash
python3 ctf_daemon.py              # Run once (writes task or processes flags)
python3 solver.py status           # View state
python3 solver.py list             # List challenges
python3 submit_flag.py <id> "flag{...}"  # Manual submission
```

## How the Agent Solves (v2.1 Loop)

```
LOOP:
  1. execute_code → run ctf_daemon.py
     → "TASK:id:category:title"  (solved!)
     → "ABORT:id:reason:elapsed" (TIMEOUT — stop current work!)
     → "PENDING:id:elapsed"      (agent busy, wait)
     → "DONE"                    (all solved, STOP)

  2. Check /tmp/ctf_abort first — if daemon aborted, don't read old task

  3. read_file /tmp/ctf_task.json
     → retry_number, attempt_history (learn from past failures!)

  4. Solve with Kali MCP tools (nmap, sqlmap, nuclei, gobuster, pwnpasi...)

  5. Periodically check /tmp/ctf_abort — stop immediately if it appears

  6. write_file /tmp/ctf_flag.txt with the flag

  7. GOTO 1
```

## Version History

| Version | Changes |
|---------|---------|
| **v2.1** | Abort signaling, attempt history, machine-readable output |
| **v2.0** | Non-blocking timeout, retry cooldown, max retries, infra failure detection |
| **v1.0** | Basic daemon-worker pattern, sequential single-task locking |

Inspired by [LingXi](https://github.com/chaojixinren/LingXi)'s concurrent task model.

## Files

| File | Purpose |
|------|---------|
| `ctf_daemon.py` | Background orchestrator (v2.1). Cron every 60s. |
| `solver.py` | One-shot solver + state management. |
| `gzctf_client.py` | GZCTF REST API client (login, fetch, submit). |
| `challenge_engine.py` | Analysis, flag extraction, strategy detection. |
| `submit_flag.py` | CLI for manual flag submission. |
| `config.env.example` | Configuration template. |

## License

MIT — use it, modify it, win CTFs with it.
