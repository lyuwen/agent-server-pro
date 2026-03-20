# Claude Code Orchestrator — Design Spec

**Date:** 2026-03-20
**Status:** Approved

---

## Overview

Upgrade the existing HTTP proxy (`main.py`) with a new orchestration service (`orchestrator.py`) that:

1. Accepts a prompt and relative working directory via a REST API
2. Spawns a dedicated proxy instance on a dynamic port for trace isolation
3. Launches Claude Code in non-interactive mode with environment patched to route all API traffic through that proxy
4. Waits for Claude Code to complete, collects the trace, and returns everything to the caller

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  orchestrator.py                     │
│                                                      │
│  POST /run { prompt, work_dir }                      │
│       │                                              │
│       ├─ 1. resolve work_dir (BASE_DIR + relative)   │
│       ├─ 2. assign job_id (uuid)                     │
│       ├─ 3. spawn proxy process  ──► main.py         │
│       │      port=0 (OS-assigned)                    │
│       │      LOG_FILE=logs/{job_id}.jsonl            │
│       │                                              │
│       ├─ 4. read assigned port from proxy stdout     │
│       │                                              │
│       ├─ 5. spawn claude subprocess                  │
│       │      cwd=resolved_work_dir                   │
│       │      env: ANTHROPIC_BASE_URL=http://...      │
│       │           HTTP_PROXY / HTTPS_PROXY           │
│       │      args: --dangerously-skip-permissions    │
│       │            --print -p "<prompt>"             │
│       │                                              │
│       ├─ 6. wait for claude to exit                  │
│       ├─ 7. terminate proxy process                  │
│       ├─ 8. read + return logs/{job_id}.jsonl        │
│       └─ 9. cleanup log file (unless KEEP_LOGS=true) │
│                                                      │
└─────────────────────────────────────────────────────┘
```

---

## Components

### `main.py` (modified)

The existing proxy receives one addition: after uvicorn binds to its OS-assigned port, it prints:

```
PROXY_PORT=<port>
```

to stdout and flushes. This is implemented by subclassing uvicorn's `Server` and overriding `startup()` to emit the line after `await super().startup()`. The bound port is read from `self.servers[0].sockets[0].getsockname()[1]`.

Everything else in `main.py` remains unchanged.

### `orchestrator.py` (new)

A FastAPI application with a single endpoint: `POST /run`.

---

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `BASE_DIR` | orchestrator's cwd | Base for resolving relative `work_dir` |
| `PORT` | `8080` | Orchestrator's listen port |
| `ANTHROPIC_API_KEY` | (required) | Forwarded to Claude subprocess |
| `PROXY_STARTUP_TIMEOUT` | `10` | Seconds to wait for proxy to print its port |
| `CLAUDE_TIMEOUT` | `300` | Seconds before Claude subprocess is killed |
| `KEEP_LOGS` | `false` | If `true`, per-job `.jsonl` files are not deleted |

---

## API

### `POST /run`

**Request body:**
```json
{
  "prompt": "Add a README to this project",
  "work_dir": "my-repo"
}
```

`work_dir` is optional — defaults to `BASE_DIR` (the service's working directory) if omitted.

**Success response (`200 OK`):**
```json
{
  "job_id": "uuid",
  "status": "completed",
  "work_dir": "/resolved/absolute/path",
  "claude_stdout": "...",
  "claude_stderr": "...",
  "claude_exit_code": 0,
  "trace": [
    {
      "id": "uuid",
      "timestamp": "2026-03-20T00:00:00Z",
      "request": { "method": "POST", "path": "/v1/messages", "body": {} },
      "response": { "status": 200, "body": {} }
    }
  ]
}
```

**Error / partial responses (all `200 OK`):**

| `status` value | Meaning |
|---|---|
| `"completed"` | Claude exited 0 |
| `"failed"` | Claude exited non-zero (trace + stderr returned) |
| `"timed_out"` | Claude exceeded `CLAUDE_TIMEOUT` (partial trace returned) |
| `"invalid_work_dir"` | Resolved path does not exist |
| `"proxy_start_failed"` | Proxy did not emit `PROXY_PORT=` within timeout |
| `"claude_not_found"` | `claude` binary not found in PATH |

**4xx errors (malformed requests):**
- `400` — `work_dir` resolves outside `BASE_DIR` or request body is invalid

---

## Port Discovery Handshake

1. Orchestrator spawns proxy with `stdout=PIPE`, `PORT=0`
2. Orchestrator reads proxy stdout line-by-line with a `PROXY_STARTUP_TIMEOUT` deadline
3. On seeing `PROXY_PORT=<n>`, orchestrator extracts the port and proceeds
4. Proxy stderr is inherited (flows to orchestrator's stderr for visibility)
5. If deadline expires without the line, proxy is killed and the run fails with `proxy_start_failed`

---

## Claude Subprocess

**Command:**
```
claude --dangerously-skip-permissions --print -p "<prompt>"
```

**Environment overrides injected:**

| Variable | Value |
|---|---|
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:{proxy_port}` |
| `HTTP_PROXY` | `http://127.0.0.1:{proxy_port}` |
| `HTTPS_PROXY` | `http://127.0.0.1:{proxy_port}` |
| `LOG_FILE` | `logs/{job_id}.jsonl` |
| `ANTHROPIC_API_KEY` | inherited from orchestrator env |

**Working directory:** resolved absolute path of `work_dir`.

**stdout/stderr:** both captured and included in the response.

**Timeout:** after `CLAUDE_TIMEOUT` seconds, both Claude and the proxy subprocess are `SIGTERM`ed, the partial trace is read, and the response includes `"status": "timed_out"`.

---

## Log File Lifecycle

- Location: `logs/{job_id}.jsonl` (relative to orchestrator's cwd)
- Created: by the proxy subprocess at first write
- Read: by orchestrator after Claude exits
- Deleted: by orchestrator after reading, unless `KEEP_LOGS=true`
- The `logs/` directory is created by the orchestrator at startup if it doesn't exist

---

## Concurrency

Each `/run` request gets its own:
- `job_id`
- proxy process on an OS-assigned port
- log file

No shared mutable state between concurrent runs. The orchestrator itself is async (FastAPI + asyncio), and subprocess lifecycle is managed with `asyncio.create_subprocess_exec` to avoid blocking the event loop.

---

## Files Changed

| File | Change |
|---|---|
| `main.py` | Add port-reporting on startup (small addition to `__main__` block) |
| `orchestrator.py` | New file — full orchestration service |
| `logs/` | New directory — created at runtime, gitignored |
