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

**Response schema — fields always present in every response:**

| Field | Type | Notes |
|---|---|---|
| `job_id` | string | Always present |
| `status` | string | Always present |
| `work_dir` | string \| null | Resolved absolute path, or `null` if resolution failed (e.g. `invalid_work_dir`) |
| `claude_stdout` | string \| null | `null` if Claude never ran |
| `claude_stderr` | string \| null | `null` if Claude never ran |
| `claude_exit_code` | int \| null | `null` if Claude never ran or was killed |
| `trace` | array \| null | `null` if proxy never started; `[]` if proxy ran but no requests were made |

**Error / partial responses (all `200 OK`):**

| `status` value | Meaning | `work_dir` | `trace` | `claude_stdout` | `claude_stderr` | `claude_exit_code` |
|---|---|---|---|---|---|---|
| `"completed"` | Claude exited 0 | string | full array | string | string | `0` |
| `"failed"` | Claude exited non-zero | string | full array | string | string | non-zero int |
| `"timed_out"` | Exceeded `CLAUDE_TIMEOUT` | string | partial array (may be `[]`) | string | string | `null` |
| `"proxy_crashed"` | Proxy exited unexpectedly mid-run | string | partial array (may be `[]`) | `null` | `null` | `null` |
| `"invalid_work_dir"` | Resolved path does not exist or is not a directory | `null` | `null` | `null` | `null` | `null` |
| `"proxy_start_failed"` | Proxy did not emit `PROXY_PORT=` in time | string | `null` | `null` | `null` | `null` |
| `"claude_not_found"` | `claude` binary not found in PATH | string | `null` | `null` | `null` | `null` |

**4xx errors (malformed requests):**
- `400` — request body is invalid
- `400` — `work_dir` resolves outside `BASE_DIR`: "outside" means the resolved path is neither equal to `BASE_DIR` nor a subdirectory of it (i.e. `not resolved.startswith(BASE_DIR + os.sep) and resolved != BASE_DIR`). Note: `work_dir` omitted or equal to `BASE_DIR` is explicitly allowed.
- `400` — `work_dir` resolves to a path that exists but is not a directory (e.g. a regular file); status `"invalid_work_dir"` is used when the path does not exist; a `400` HTTP error is returned when the path exists but is not a directory

---

## Port Discovery Handshake

1. Orchestrator spawns proxy with `stdout=PIPE`, `stderr=inherited`, `PORT=0`
2. Uvicorn's own startup banners go to **stderr**; stdout is reserved exclusively for the `PROXY_PORT=` line. The `main.py` modification suppresses all other stdout output and emits only this one line
3. Orchestrator reads proxy stdout line-by-line with a `PROXY_STARTUP_TIMEOUT` deadline; it scans each line for the prefix `PROXY_PORT=` and extracts the port — the line may appear anywhere in stdout (robustness), but in practice will be the first and only stdout line
4. On seeing `PROXY_PORT=<n>`, orchestrator extracts the port and proceeds
5. If deadline expires without the line, proxy is `SIGTERM`ed, then `.wait()`ed (to avoid zombies), any partial log file is deleted, and the run fails with `proxy_start_failed`; all fields except `job_id`, `status`, and `work_dir` are `null` in the response

---

## Claude Subprocess

**Command:**
```
claude --dangerously-skip-permissions --print -p "<prompt>"
```

**Proxy subprocess environment overrides:**

| Variable | Value | Purpose |
|---|---|---|
| `PORT` | `0` | OS assigns a free port |
| `LOG_FILE` | `logs/{job_id}.jsonl` | Per-job trace file |
| `API_BASE_URL` | inherited | Upstream API target |

**Claude subprocess environment overrides:**

| Variable | Value |
|---|---|
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:{proxy_port}` |
| `HTTP_PROXY` | `http://127.0.0.1:{proxy_port}` |
| `HTTPS_PROXY` | `http://127.0.0.1:{proxy_port}` |
| `ANTHROPIC_API_KEY` | inherited from orchestrator env |

**Working directory:** resolved absolute path of `work_dir`.

**stdout/stderr:** both captured and included in the response.

**Timeout & termination:** after `CLAUDE_TIMEOUT` seconds, the orchestrator sends `SIGTERM` to Claude and the proxy, waits up to 5 seconds for graceful exit, then sends `SIGKILL` to any still-running processes. Both processes are `.wait()`ed to avoid zombies. The partial trace is read, and the response includes `"status": "timed_out"`.

**Normal-path proxy shutdown:** after Claude exits normally (non-timeout), the orchestrator sends `SIGTERM` to the proxy and calls `.wait()` with a 5-second timeout. If the proxy has not exited after 5 seconds, `SIGKILL` is sent followed by another `.wait()`. This matches the timeout-path escalation sequence.

**Proxy crash mid-run:** the orchestrator monitors the proxy process liveness concurrently with Claude execution (via `asyncio.wait` on both tasks). If the proxy exits unexpectedly before Claude finishes, the orchestrator immediately sends `SIGTERM` → `SIGKILL` to Claude (same 5s escalation), reads any partial trace from the log file (may be `[]` if the file doesn't exist yet), and returns `"status": "proxy_crashed"`.

---

## Log File Lifecycle

- Location: `logs/{job_id}.jsonl` (relative to orchestrator's cwd)
- Created: by the proxy subprocess at first write (may not exist if proxy made no writes)
- Read: by orchestrator after Claude exits; if the file does not exist, `trace` is `[]` (not an error)
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
| `main.py` | Subclass `uvicorn.Server`, override `startup()` to print `PROXY_PORT=<n>` to stdout after binding |
| `orchestrator.py` | New file — full orchestration service |
| `logs/` | New directory — created at runtime, gitignored |
