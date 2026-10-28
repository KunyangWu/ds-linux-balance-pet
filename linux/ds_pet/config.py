import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


APP_NAME = "ds-linux-balance-pet"


def _xdg(name, fallback):
    value = os.environ.get(name)
    return Path(value) if value else Path.home() / fallback


@dataclass
class Paths:
    config_dir: Path = field(default_factory=lambda: _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME)
    state_dir: Path = field(default_factory=lambda: _xdg("XDG_STATE_HOME", ".local/state") / APP_NAME)
    runtime_dir: Path = field(default_factory=lambda: Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/{APP_NAME}-{os.getuid()}")) / APP_NAME)
    assets_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "assets")

    @property
    def config_file(self):
        return self.config_dir / "config.json"

    @property
    def state_file(self):
        return self.state_dir / "pet-state.json"

    @property
    def status_file(self):
        return self.runtime_dir / "status.json"

    @property
    def lock_file(self):
        return self.runtime_dir / "instance.lock"

    @property
    def balance_log(self):
        return self.state_dir / "balance-log.csv"

    def ensure(self):
        for path in (self.config_dir, self.state_dir, self.runtime_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)


@dataclass
class Config:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    balance_url: str = ""
    refresh_seconds: int = 30
    idle_sleep_seconds: int = 150
    log_interval_seconds: int = 1800
    low_threshold: float | None = None
    low_currency: str = "CNY"
    show_stats: bool = True
    topmost: bool = True
    no_activate: bool = True
    font_family: str = "Noto Sans CJK SC"
    request_timeout: float = 12.0
    scale: float = 1.0
    animation_mode: str = "compact"
    compact_scale_factor: float = 1.6

    @classmethod
    def load(cls, paths):
        data = {}
        try:
            data = json.loads(paths.config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if not isinstance(data, dict):
            data = {}
        allowed = {name for name in cls.__dataclass_fields__ if name != "api_key"}
        values = {key: value for key, value in data.items() if key in allowed}
        config = cls(**values)
        key = data.get("api_key")
        if isinstance(key, str):
            config.api_key = key
        return config

    def save(self, paths):
        paths.ensure()
        payload = asdict(self)
        payload["api_key"] = self.api_key
        atomic_write_json(paths.config_file, payload)
        try:
            paths.config_file.chmod(0o600)
        except OSError:
            pass

    def effective_api_key(self):
        return os.environ.get("DEEPSEEK_API_KEY", "").strip() or self.api_key.strip()

    def effective_balance_url(self):
        override = os.environ.get("BALANCE_URL", "").strip()
        if override:
            return override
        if self.balance_url.strip():
            return self.balance_url.strip()
        return self.base_url.rstrip("/") + "/user/balance"

    def effective_base_url(self):
        override = os.environ.get("DEEPSEEK_BASE_URL", "").strip()
        return override or self.base_url

    def alert_threshold(self):
        if self.low_threshold is not None:
            return float(self.low_threshold), self.low_currency.upper()
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        try:
            data = json.loads((codex_home / "balance-alert.json").read_text(encoding="utf-8"))
            threshold = data.get("threshold")
            if isinstance(threshold, (int, float)):
                currency = str(data.get("currency") or "CNY").upper()
                return float(threshold), currency
        except (OSError, ValueError, TypeError):
            pass
        return None, self.low_currency.upper()


def atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def read_json(path, default=None):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {} if default is None else default
