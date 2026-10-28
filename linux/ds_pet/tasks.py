import codecs
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_STATUS = {"inProgress", "completed", "failed", "interrupted", "cancelled", "canceled"}


def _source_info(value, thread_source):
    source = value
    if isinstance(value, str):
        try:
            source = json.loads(value)
        except ValueError:
            source = value
    subagent = source.get("subagent") if isinstance(source, dict) else None
    parent = None
    if isinstance(subagent, dict):
        spawn = subagent.get("thread_spawn") or subagent.get("threadSpawn") or {}
        parent = spawn.get("parent_thread_id") or spawn.get("parentThreadId") or subagent.get("parent_thread_id")
    is_subagent = isinstance(subagent, dict) or thread_source in ("subagent", "agent")
    allowed = {"cli", "vscode", "appServer", "exec"}
    kind = "subagent" if is_subagent else (source if isinstance(source, str) and source in allowed else "unknown")
    return {
        "source_kind": kind,
        "is_subagent": is_subagent,
        "parent_thread_id": parent if isinstance(parent, str) else None,
    }


def read_task_store(codex_home):
    history = state = None
    try:
        home = Path(codex_home)
        history = sqlite3.connect(f"file:{home / 'thread_history_1.sqlite'}?mode=ro", uri=True)
        state = sqlite3.connect(f"file:{home / 'state_5.sqlite'}?mode=ro", uri=True)
        history.row_factory = sqlite3.Row
        state.row_factory = sqlite3.Row
        for connection in (history, state):
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=800")
        metadata = {
            row["id"]: row
            for row in state.execute(
                "SELECT id, rollout_path, source, thread_source, archived FROM threads"
            ).fetchall()
        }
        rows = history.execute(
            """
            WITH latest AS (
              SELECT thread_id, turn_id, root_turn_id, status, started_at, completed_at,
                     ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY started_at DESC, rollout_ordinal DESC) AS position
              FROM thread_turns
            )
            SELECT thread_id, turn_id, root_turn_id, status, started_at, completed_at
            FROM latest WHERE position=1
            """
        ).fetchall()
        result = []
        for row in rows:
            meta = metadata.get(row["thread_id"])
            status = row["status"] if row["status"] in ALLOWED_STATUS else "unknown"
            result.append(
                {
                    "thread_id": row["thread_id"],
                    "turn_id": row["turn_id"],
                    "root_turn_id": row["root_turn_id"],
                    "status": status,
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "archived": bool(meta and meta["archived"] == 1),
                    "rollout_path": meta["rollout_path"] if meta and isinstance(meta["rollout_path"], str) else None,
                    **_source_info(meta["source"] if meta else None, meta["thread_source"] if meta else None),
                }
            )
        return {"available": True, "observed_at": datetime.now(timezone.utc).isoformat(), "threads": result}
    except (OSError, sqlite3.Error):
        return {"available": False, "observed_at": datetime.now(timezone.utc).isoformat(), "threads": []}
    finally:
        if history is not None:
            history.close()
        if state is not None:
            state.close()


def _proc_start_time(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2:].split()
        ticks = int(fields[19])
        clock = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        btime = 0
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        return datetime.fromtimestamp(btime + ticks / clock, timezone.utc)
    except (OSError, ValueError, IndexError):
        return None


def _runtime():
    starts = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False, None
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            continue
        if comm != "codex":
            continue
        started = _proc_start_time(pid)
        if started is not None:
            starts.append(started)
    if not starts:
        return False, None
    return True, min(starts)


class FileState:
    __slots__ = (
        "path", "offset", "carry", "decoder", "thread_id", "parent_thread_id", "is_subagent",
        "meta_known", "turn_id", "root_turn_id", "started_at", "active", "live_start", "phase",
        "initial", "modified_at", "backlog",
    )

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.carry = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.thread_id = None
        self.parent_thread_id = None
        self.is_subagent = False
        self.meta_known = False
        self.turn_id = None
        self.root_turn_id = None
        self.started_at = None
        self.active = False
        self.live_start = False
        self.phase = "unverified"
        self.initial = True
        self.modified_at = 0.0
        self.backlog = False


class TaskActivity:
    def __init__(self, sessions_dir, codex_home):
        self.sessions_dir = Path(sessions_dir)
        self.codex_home = Path(codex_home)
        self.files = {}
        self.store_rows = {}
        self.store_available = False
        self.last_store_at = 0.0
        self.runtime_running = False
        self.runtime_started_at = None
        self.last_runtime_at = 0.0
        self.result = {"is_busy": False, "active_count": 0, "phase": "idle", "task_id": None}

    def _update_store(self):
        now = time.monotonic()
        if now - self.last_store_at < 2:
            return
        self.last_store_at = now
        snapshot = read_task_store(self.codex_home)
        self.store_available = bool(snapshot.get("available"))
        self.store_rows = {}
        if self.store_available:
            for row in snapshot.get("threads", []):
                if row.get("thread_id"):
                    self.store_rows[str(row["thread_id"])] = row

    def _update_runtime(self):
        now = time.monotonic()
        if now - self.last_runtime_at < 5:
            return
        self.last_runtime_at = now
        self.runtime_running, self.runtime_started_at = _runtime()

    def _read_event(self, file_state, line, historical):
        if len(line) > 8192:
            return
        if '"session_meta"' not in line and '"event_msg"' not in line and '"response_item"' not in line:
            return
        try:
            record = json.loads(line)
        except ValueError:
            return
        payload = record.get("payload") or {}
        record_type = record.get("type")
        if record_type == "session_meta":
            file_state.thread_id = str(payload.get("id") or payload.get("session_id") or "")
            file_state.parent_thread_id = str(payload.get("parent_thread_id") or "")
            source = payload.get("source")
            if isinstance(source, dict):
                spawn = (source.get("subagent") or {}).get("thread_spawn") or (source.get("subagent") or {}).get("threadSpawn") or {}
                if not file_state.parent_thread_id:
                    file_state.parent_thread_id = str(spawn.get("parent_thread_id") or spawn.get("parentThreadId") or "")
            file_state.is_subagent = bool(file_state.parent_thread_id or (isinstance(source, dict) and source.get("subagent") is not None))
            file_state.meta_known = True
            return
        if record_type == "event_msg":
            event_type = payload.get("type")
            if event_type == "task_started":
                file_state.turn_id = str(payload.get("turn_id") or "")
                file_state.root_turn_id = str(payload.get("root_turn_id") or "")
                raw_started = payload.get("started_at") or record.get("timestamp")
                file_state.started_at = _parse_time(raw_started)
                file_state.active = True
                file_state.live_start = not historical
                file_state.phase = "thinking"
            elif event_type in ("task_complete", "turn_aborted", "task_aborted"):
                if (
                    not payload.get("turn_id")
                    or not file_state.turn_id
                    or payload.get("turn_id") == file_state.turn_id
                    or (payload.get("root_turn_id") and payload.get("root_turn_id") == file_state.root_turn_id)
                ):
                    file_state.active = False
                    file_state.live_start = False
                    file_state.phase = "completed" if event_type == "task_complete" else "interrupted"
            elif event_type == "item_completed" and file_state.active:
                item = payload.get("item") or {}
                if item.get("type") in ("Reasoning", "reasoning"):
                    file_state.phase = "thinking"
            return
        if record_type == "response_item" and file_state.active:
            item_type = payload.get("type")
            if item_type in ("function_call", "custom_tool_call"):
                file_state.phase = "working"
            elif item_type in ("function_call_output", "custom_tool_call_output", "reasoning"):
                file_state.phase = "thinking"
            elif item_type in ("message", "agent_message") and payload.get("role") == "assistant" and payload.get("channel") == "final":
                file_state.active = False
                file_state.live_start = False
                file_state.phase = "completed"

    def _read_delta(self, path):
        try:
            stat = path.stat()
        except OSError:
            return
        file_state = self.files.get(str(path))
        if file_state is None or stat.st_size < file_state.offset:
            file_state = FileState(path)
            self.files[str(path)] = file_state
        if stat.st_size == file_state.offset:
            return
        try:
            with path.open("rb") as handle:
                handle.seek(file_state.offset)
                chunk = handle.read(min(2097152, stat.st_size - file_state.offset))
                file_state.offset = handle.tell()
                file_state.backlog = file_state.offset < stat.st_size
        except OSError:
            return
        if not chunk:
            return
        text = file_state.decoder.decode(chunk, final=False)
        lines = (file_state.carry + text).split("\n")
        file_state.carry = lines[-1]
        for line in lines[:-1]:
            self._read_event(file_state, line.rstrip("\r"), file_state.initial)
        if not file_state.backlog:
            file_state.initial = False
        file_state.modified_at = stat.st_mtime

    def _refresh_files(self):
        try:
            files = sorted(
                self.sessions_dir.rglob("*.jsonl"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for path in files:
            self._read_delta(path)

    def update(self):
        self._update_store()
        self._update_runtime()
        self._refresh_files()
        by_thread = {}
        for file_state in self.files.values():
            if not file_state.meta_known or not file_state.thread_id:
                continue
            existing = by_thread.get(file_state.thread_id)
            if existing is None or file_state.modified_at > existing.modified_at:
                by_thread[file_state.thread_id] = file_state
        active = {}
        unverified = False
        if self.runtime_running:
            thread_ids = set(by_thread) | set(self.store_rows)
            for thread_id in thread_ids:
                local = by_thread.get(thread_id)
                row = self.store_rows.get(thread_id)
                if row and row.get("archived"):
                    continue
                running = False
                known = False
                if self.store_available and row:
                    known = True
                    running = row.get("status") == "inProgress"
                    if running and self.runtime_started_at and row.get("started_at"):
                        try:
                            started = datetime.fromtimestamp(int(row["started_at"]), timezone.utc)
                            if started < self.runtime_started_at:
                                running = False
                                known = False
                                unverified = True
                        except (TypeError, ValueError, OSError):
                            pass
                    if local and local.turn_id == row.get("turn_id") and not local.active:
                        running = False
                if (
                    local
                    and local.active
                    and local.live_start
                    and (not known or not row or local.turn_id != row.get("turn_id"))
                ):
                    running = True
                if not running:
                    if local and local.active and not known:
                        unverified = True
                    continue
                is_subagent = (local and local.is_subagent) or (row and row.get("is_subagent"))
                root_id = thread_id
                if is_subagent:
                    parent = (local.parent_thread_id if local and local.parent_thread_id else (row.get("parent_thread_id") if row else ""))
                    seen = set()
                    while parent and parent not in seen:
                        seen.add(parent)
                        root_id = parent
                        ancestor = by_thread.get(parent)
                        ancestor_row = self.store_rows.get(parent)
                        parent = (
                            ancestor.parent_thread_id if ancestor and ancestor.parent_thread_id
                            else (ancestor_row.get("parent_thread_id") if ancestor_row else "")
                        )
                task_phase = local.phase if local and local.active else "working"
                task_id = (local.turn_id if local and local.turn_id else (row.get("turn_id") if row else None))
                if root_id not in active or task_phase == "working":
                    active[root_id] = {"phase": task_phase, "task_id": task_id}
        if active:
            phase = "working" if any(item["phase"] == "working" for item in active.values()) else "thinking"
        elif not self.store_available or unverified:
            phase = "unverified"
        else:
            phase = "idle"
        task_id = None
        if len(active) == 1:
            task_id = next(iter(active.values()))["task_id"]
        self.result = {
            "is_busy": bool(active),
            "active_count": len(active),
            "phase": phase,
            "task_id": task_id,
        }
        return self.result


def _parse_time(value):
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
