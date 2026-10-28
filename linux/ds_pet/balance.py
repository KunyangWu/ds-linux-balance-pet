import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

AMOUNT_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class BalanceError(Exception):
    pass


@dataclass
class BalanceResult:
    text: str
    low: bool
    value: Decimal | None
    currency: str | None
    provider: str
    detail: str
    queried_at: str
    data: dict

    def payload(self):
        return {
            "kind": "balance",
            "provider": self.provider,
            "queried_at": self.queried_at,
            "data": self.data,
        }


def currency_symbol(currency):
    if currency == "CNY":
        return "¥"
    if currency == "USD":
        return "$"
    return f"{currency} "


def _safe_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ("http", "https"):
            return ""
        if not parsed.netloc or parsed.username or parsed.password:
            return ""
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return ""


def _clean_amount(value):
    text = str(value)
    if not AMOUNT_RE.match(text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _clean_payload(payload):
    if not isinstance(payload, dict):
        raise BalanceError("余额接口未返回可识别的余额数据。")
    if not isinstance(payload.get("is_available"), bool):
        raise BalanceError("余额接口未返回可识别的余额数据。")
    infos = payload.get("balance_infos")
    if not isinstance(infos, list):
        infos = []
    clean = []
    for entry in infos:
        if not isinstance(entry, dict):
            continue
        currency = entry.get("currency")
        if not isinstance(currency, str) or not CURRENCY_RE.match(currency):
            continue
        total = _clean_amount(entry.get("total_balance"))
        topped = _clean_amount(entry.get("topped_up_balance"))
        granted = _clean_amount(entry.get("granted_balance"))
        if total is None or topped is None or granted is None:
            continue
        clean.append(
            {
                "currency": currency,
                "total_balance": str(total),
                "topped_up_balance": str(topped),
                "granted_balance": str(granted),
            }
        )
    if not clean:
        raise BalanceError("余额接口未返回可识别的余额数据。")
    return {"is_available": payload["is_available"], "balance_infos": clean}


def fetch_balance(config):
    key = config.effective_api_key()
    if not key:
        raise BalanceError("未配置 DeepSeek API 密钥。")
    url = _safe_url(config.effective_balance_url())
    if not url:
        raise BalanceError("余额接口地址无效。")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
            "User-Agent": "ds-linux-balance-pet/0.1.0",
        },
        method="GET",
    )
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=config.request_timeout, context=context) as response:
            body = response.read(1024 * 1024)
    except urllib.error.HTTPError as error:
        raise BalanceError(f"余额接口请求失败。（HTTP {error.code}）") from None
    except urllib.error.URLError:
        raise BalanceError("无法连接余额接口。") from None
    except TimeoutError:
        raise BalanceError("实时查询超时，请稍后重试。") from None
    except OSError:
        raise BalanceError("无法连接余额接口。") from None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BalanceError("余额接口未返回可识别的余额数据。") from None
    data = _clean_payload(payload)
    infos = data["balance_infos"]
    primary = next((entry for entry in infos if entry["currency"] == "CNY"), None)
    if primary is None:
        primary = next((entry for entry in infos if entry["currency"] == "USD"), None)
    if primary is None:
        primary = infos[0]
    currency = primary["currency"]
    total = Decimal(primary["total_balance"])
    threshold, threshold_currency = config.alert_threshold()
    low = not data["is_available"]
    if threshold is not None and threshold_currency == currency:
        low = low or float(total) < threshold
    symbol = currency_symbol(currency)
    text = symbol + primary["total_balance"]
    detail = (
        f"DeepSeek 余额 {symbol}{primary['total_balance']}"
        f"（充值 {symbol}{primary['topped_up_balance']}，赠送 {symbol}{primary['granted_balance']}）"
    )
    return BalanceResult(
        text=text,
        low=low,
        value=total,
        currency=currency,
        provider="DeepSeek",
        detail=detail,
        queried_at=datetime.now(timezone.utc).isoformat(),
        data=data,
    )
