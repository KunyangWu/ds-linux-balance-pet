import codecs
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

class UsageReader:
    def __init__(self, sessions_dir):
        self.sessions_dir = Path(sessions_dir)
        self.file = None
        self.offset = 0
        self.carry = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.requests = 0
        self.input = 0
        self.cached = 0
        self.output = 0
        self.reasoning = 0
        self.recent = []
        self.last_total = None
        self.caught_up = False
        self.seen_events = set()
        self.meta_cache = {}

    def _reset_file(self, path):
        self.file = path
        self.offset = 0
        self.carry = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.requests = 0
        self.input = 0
        self.cached = 0
        self.output = 0
        self.reasoning = 0
        self.recent = []
        self.last_total = None
        self.caught_up = False
        self.seen_events = set()

    def _classify(self, path):
        try:
            stat = path.stat()
        except OSError:
            return "unknown"
        cached = self.meta_cache.get(str(path))
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
        classification = "unknown"
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                line = handle.readline(1048576)
            record = json.loads(line)
            if record.get("type") == "session_meta":
                payload = record.get("payload") or {}
                source = payload.get("source")
                is_subagent = (
                    payload.get("parent_thread_id") is not None
                    or (not isinstance(source, str) and isinstance(source, dict) and source.get("subagent") is not None)
                    or source == "subagent"
                )
                classification = "subagent" if is_subagent else "primary"
        except (OSError, ValueError):
            pass
        self.meta_cache[str(path)] = (stat.st_mtime_ns, stat.st_size, classification)
        return classification

    def newest_primary(self):
        fallback = None
        try:
            files = sorted(self.sessions_dir.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            return None
        for path in files:
            classification = self._classify(path)
            if classification == "primary":
                return path
            if classification == "unknown" and fallback is None:
                fallback = path
        return fallback

    @staticmethod
    def _numbers(value):
        if not isinstance(value, dict):
            return None
        result = {}
        for name, key in (
            ("input", "input_tokens"),
            ("cached", "cached_input_tokens"),
            ("output", "output_tokens"),
            ("reasoning", "reasoning_output_tokens"),
        ):
            raw = value.get(key)
            if raw is None:
                number = 0
            else:
                try:
                    number = int(raw)
                except (TypeError, ValueError):
                    return None
                if number < 0:
                    return None
            result[name] = number
        result["cached"] = min(result["cached"], result["input"])
        result["reasoning"] = min(result["reasoning"], result["output"])
        return result

    @staticmethod
    def _signature(record, numbers):
        payload = record.get("payload") or {}
        identity = payload.get("request_id") or payload.get("id") or record.get("id") or record.get("timestamp")
        return "|".join(str(part) for part in (
            identity,
            numbers["input"],
            numbers["cached"],
            numbers["output"],
            numbers["reasoning"],
        ))

    def _add_delta(self, delta, timestamp):
        if sum(delta.values()) <= 0:
            return
        self.input += int(delta["input"])
        self.cached += int(delta["cached"])
        self.output += int(delta["output"])
        self.reasoning += int(delta["reasoning"])
        self.requests += 1
        if delta["output"] <= 0 or not timestamp:
            return
        try:
            if isinstance(timestamp, datetime):
                at = timestamp
            else:
                at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            at = at.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return
        self.recent.append((at, int(delta["output"])))
        if len(self.recent) > 500:
            self.recent = self.recent[-500:]

    def _read_event(self, line):
        if '"token_count"' not in line and '"token_usage_record"' not in line:
            return
        try:
            record = json.loads(line)
        except ValueError:
            return
        if record.get("type") != "event_msg":
            return
        payload = record.get("payload") or {}
        if payload.get("type") == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                return
            total = self._numbers(info.get("total_token_usage"))
            if total is not None:
                previous = {
                    "input": self.input,
                    "cached": self.cached,
                    "output": self.output,
                    "reasoning": self.reasoning,
                }
                if any(total[name] < previous[name] for name in previous):
                    return
                delta = {name: total[name] - previous[name] for name in previous}
                self.last_total = total
                last = self._numbers(info.get("last_token_usage"))
                if last is not None:
                    self.seen_events.add(self._signature(record, last))
                self._add_delta(delta, record.get("timestamp"))
                return
            numbers = self._numbers(info.get("last_token_usage"))
        elif payload.get("type") == "token_usage_record":
            numbers = self._numbers(payload.get("usage"))
        else:
            return
        if numbers is None:
            return
        signature = self._signature(record, numbers)
        if signature in self.seen_events:
            return
        self.seen_events.add(signature)
        self._add_delta(numbers, record.get("timestamp"))

    def _read_bytes(self, path, limit):
        try:
            with path.open("rb") as handle:
                if path.stat().st_size < self.offset:
                    self._reset_file(path)
                handle.seek(self.offset)
                chunk = handle.read(limit)
                self.offset = handle.tell()
                self.caught_up = self.offset >= path.stat().st_size
        except OSError:
            return
        if not chunk:
            return
        text = self.decoder.decode(chunk, final=False)
        lines = (self.carry + text).split("\n")
        self.carry = lines[-1]
        for line in lines[:-1]:
            self._read_event(line.rstrip("\r"))

    def update(self, selected_file_only=False):
        path = self.file if selected_file_only else self.newest_primary()
        if path is None:
            return
        if selected_file_only:
            if not self.caught_up or self.file is None:
                return
            try:
                if self.file.stat().st_size == self.offset:
                    return
            except OSError:
                return
        if self.file != path:
            self._reset_file(path)
        self._read_bytes(path, 262144 if selected_file_only else 4194304)

    def rate(self, seconds=30):
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        self.recent = [(at, count) for at, count in self.recent if at >= cutoff]
        return sum(count for _, count in self.recent) / float(seconds)

    def cache_ratio(self):
        return self.cached / self.input if self.input > 0 else 0.0


def format_count(value):
    if value >= 1000000:
        return f"{value / 1000000:.1f}M"
    if value >= 1000:
        return f"{value / 1000:.0f}k"
    return f"{value:.0f}"

