# CTF Daemon · Hermes-native autonomous competition framework

[中文版](README_CN.md)

> *You sleep. The daemon works.*

A Hermes Agent-native framework for fully autonomous CTF competition solving.
Multi-slot concurrent dispatch, isolated per-challenge workdirs, full container
lifecycle management, LLM-driven challenge selection, platform-verified
submission pipeline. Designed to run unattended for the full duration of a
competition — from opening bell to scoreboard freeze.

**Runtime**: Hermes Agent + Kali-Security-MCP + GZCTF (or compatible platform)

```text
┌──────────────────────────────────────────────────────────────────┐
│                        HERMES AGENT                               │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │              CTF DAEMON (orchestrator)                      │  │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐                    │  │
│  │  │ Slot 0  │  │ Slot 1  │  │ Slot 2  │  … up to N slots   │  │
│  │  │ workdir │  │ workdir │  │ workdir │    isolated per ch  │  │
│  │  │ timeout │  │ timeout │  │ timeout │    independent retry │  │
│  │  └────┬────┘  └────┬────┘  └────┬────┘                    │  │
│  │       │            │            │                           │  │
│  │       ▼            ▼            ▼                           │  │
│  │  ┌─────────────────────────────────────────────────────┐   │  │
│  │  │              Kali-Security-MCP (241 tools)           │   │  │
│  │  │  nmap · sqlmap · nuclei · hydra · msf · pwnpasi     │   │  │
│  │  │  gobuster · ffuf · hashcat · john · radare2 …       │   │  │
│  │  └─────────────────────────────────────────────────────┘   │  │
│  └────────────────────────────────────────────────────────────┘  │
│       │                                                          │
│       ▼                                                          │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                   GZCTF Platform                            │  │
│  │  login · challenge fetch · container mgmt · flag submit     │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

## Stack

| Component | Role | Repository |
|-----------|------|------------|
| **Hermes Agent** | AI reasoning engine — reads slot tasks, calls tools, writes flags | [nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent) |
| **Kali-Security-MCP** | Weapon arsenal — 241 Kali tools exposed via MCP protocol | [SeaC-25/Kali-Security-MCP](https://github.com/SeaC-25/Kali-Security-MCP) |
| **CTF Daemon** | Orchestration core — dispatch, retry, cooldown, container lifecycle, platform sync | this repo |

## Capabilities

| Capability | Detail |
|------------|--------|
| **Concurrent dispatch** | Up to `CTF_CONCURRENT_SLOTS` challenges solved in parallel. One stuck slot does not block others. |
| **Isolated workdirs** | Each challenge gets `/tmp/ctf_{title}_{id}/` — attachments, scripts, artifacts scoped per challenge. Zero file collision across 40+ challenges. |
| **Container lifecycle** | Auto create, extend (>30 min), health-check (TCP connect + HTTP liveness probe), delete on free. Dead containers detected in ~9s with no retry penalty. |
| **LLM-driven selection** | Daemon outputs challenge menu → Hermes LLM reads, analyzes difficulty, writes priority ordering → daemon dispatches by priority. No hardcoded heuristics. |
| **Escalating backoff** | Failed challenges cool down: 60s → 120s → 240s → … → 1800s. Infrastructure failures (API 502, container death, rate limit) do not count as retries. |
| **Platform-verified submission** | `submit_and_record()` polls status endpoint. Only marks `solved` on `Accepted`/`AlreadySolved`. Cross-references team submissions endpoint — never trusts local state alone. |
| **Cross-challenge guard** | Flag files are JSON with `challenge_id`. Daemon verifies slot's current `ch_id` matches before submission. Prevents wrong-challenge flag submission. |
| **Session resilience** | Auto re-login on cookie expiry. API retries with exponential backoff (3 attempts). Atomic file writes for menu/selection to avoid cron race. |
| **Attempt history** | Records tools used, summaries, errors per challenge. Injected into retry context so the LLM learns from prior failures. |

## Quick Start

```bash
# 1. Clone
git clone https://github.com/chaojixinren/ctf-daemon.git
cd ctf-daemon
pip install requests

# 2. Configure
cp config.env.example config.env
# Edit: GZCTF_BASE_URL, GZCTF_USERNAME, GZCTF_PASSWORD

# 3. Run once (manual)
python3 ctf_daemon.py

# 4. Deploy as Hermes cron (autonomous)
hermes cronjob create \
  --name "CTF Daemon" \
  --schedule "every 1m" \
  --prompt "Run the CTF daemon: cd /path/to/ctf-daemon && python3 ctf_daemon.py. If MENU: read menu, pick 3 best challenges, write selection. Re-run. Solve dispatched slots. Write flags in JSON: {\"challenge_id\": N, \"flag\": \"...\"}"
```

## Configuration

```ini
# Platform credentials
GZCTF_BASE_URL=http://your-gzctf-server:8080
GZCTF_USERNAME=your_username
GZCTF_PASSWORD=your_password
GZCTF_GAME_ID=0              # 0 = auto-discover
GZCTF_TEAM_NAME=AI_Solver

# Daemon tuning
CTF_CONCURRENT_SLOTS=3        # parallel challenge slots
CTF_TASK_TIMEOUT_SECONDS=600  # per-slot timeout before abort
CTF_MAX_RETRIES_PER_CHALLENGE=4
CTF_BASE_RETRY_COOLDOWN=60    # base backoff (seconds)
CTF_MAX_RETRY_COOLDOWN=1800   # backoff ceiling (seconds)
CTF_WORKDIR_BASE=/tmp         # per-challenge workdir root
```

## Agent Loop

The Hermes agent executes this loop until the daemon outputs `DONE` (all challenges platform-verified solved):

```text
LOOP:
  1. Run daemon: python3 ctf_daemon.py
     Parse output line:
     "MENU:N:..."        → Daemon wrote /tmp/ctf_menu.jsonl. GOTO step 2.
     "DISPATCH:N/3:..."   → Slots filled. GOTO step 3.
     "BUSY:3/3:..."       → All slots busy. GOTO step 3.
     "WAITING:N..."       → Challenges in cooldown. Wait 60s. GOTO 1.
     "DONE"               → All verified solved on platform. STOP.

  2. LLM SELECTION:
     a. read_file /tmp/ctf_menu.jsonl
     b. Analyze: DynamicAttachment > DynamicContainer, check content_preview for flags
     c. Pick 3 most promising challenges
     d. write_file /tmp/ctf_selection.json:
        {"challenge_ids": ["id1","id2","id3"], "reasoning": "..."}
     e. GOTO 1

  3. SOLVE dispatched slots:
     a. read_file /tmp/ctf_tasks/slot_N.json
     b. Check /tmp/ctf_tasks/abort_N first — if exists, skip this slot
     c. All work in the "workdir" field's directory
     d. Use Kali MCP tools (mcp_kali_*) to solve
     e. attempt_history shows prior failures — do not repeat
     f. Flag found → write_file /tmp/ctf_tasks/flag_N.txt:
        {"challenge_id": <N>, "flag": "dutctf{...}"}

  4. GOTO 1
```

## Filesystem Layout

```text
/tmp/ctf_tasks/               # Slot directory (daemon-managed)
├── slot_0.json               # Challenge task with workdir, tools, history
├── slot_1.json
├── flag_0.txt                # Agent writes JSON: {"challenge_id":N, "flag":"..."}
├── flag_1.txt
├── abort_0                   # Daemon writes: stop working this slot
└── abort_1

/tmp/ctf_{title}_{id}/        # Per-challenge isolated workdir
├── <attachment files>        # Downloaded by daemon
├── exploit.py                # Agent-written scripts
└── output.txt                # Tool outputs

/tmp/ctf_menu.jsonl           # Daemon → LLM: eligible challenges
/tmp/ctf_selection.json       # LLM → Daemon: priority ordering

~/.hermes/ctf_state.json      # Persistent state (solved, retries, history)
```

### Workdir Naming

| Challenge Title | Workdir |
|-----------------|---------|
| `Hello_World` (ID:1) | `/tmp/ctf_hello_world_1/` |
| `!!! welcome !!!` (ID:3) | `/tmp/ctf_welcome_3/` |
| `签到` (ID:7) | `/tmp/ctf_challenge_7/` |
| `变异凯撒` (ID:9) | `/tmp/ctf_challenge_9/` |

Chinese-only titles that sanitize to empty string are disambiguated by `_{id}` suffix.

## Manual Operations

```bash
python3 ctf_daemon.py              # Run one daemon tick
python3 solver.py status           # Dump state file
python3 solver.py list             # List all challenges with status
python3 submit_flag.py <id> "flag{...}"  # Manual flag submission
```

## Module Map

| File | Purpose |
|------|---------|
| `ctf_daemon.py` | Orchestrator: login, fetch, LLM selection, dispatch, submit, container lifecycle, health checks |
| `gzctf_client.py` | GZCTF REST API client — auth, challenges, containers, submissions, retry logic |
| `state.py` | Persistent state: solved/retries/cooldowns/slots/history. Env-overridable paths. |
| `challenge_engine.py` | Challenge analysis, flag extraction, strategy determination, file categorization |
| `solver.py` | Standalone one-shot solver + `submit_and_record()` utility |
| `submit_flag.py` | CLI manual flag submitter |

## Version History

| Version | Changes |
|---------|---------|
| **v3.5.1** | JSON flag format with `challenge_id` guard. Container HTTP liveness probe. Per-team platform sync via `/submissions`. Session auto re-login. Bare-port `instanceEntry` fix. Structured container errors. Atomic menu/selection writes. API retry with exponential backoff. Enhanced flag filter. N+1 query optimization. |
| **v3.4.4** | Pure-Chinese title workdir collision fix via `_{id}` suffix |
| **v3.4.3** | Full container lifecycle: create/extend/health-check/delete |
| **v3.4** | Per-challenge isolated workdirs |
| **v3.3** | Platform cross-verification. Removed permanent failure — always retry. |
| **v3.2** | LLM-driven challenge selection via menu/selection protocol |
| **v3.1** | Multi-slot concurrency. Code quality fixes. Test suite. |
| **v2.1** | Abort signaling + attempt history |
| **v2.0** | Timeout, backoff cooldown, max retries, infrastructure fault tolerance |

## Credits

- Concurrency model inspired by [LingXi (灵犀)](https://github.com/chaojixinren/LingXi)
- GZCTF API patterns from [Misuzu](https://github.com/TechnickOcean/Misuzu)
- Tool backend: [Kali-Security-MCP](https://github.com/SeaC-25/Kali-Security-MCP) by SeaC-25
- AI runtime: [Hermes Agent](https://github.com/nousresearch/hermes-agent) by Nous Research

## License

MIT
