"""Outbound MAX Bot API notifications for completed exchange runs."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from observability import ERROR_TRACE_LOGGER, redact_text


MAX_API_BASE = "https://platform-api2.max.ru"
DEFAULT_STATUSES = {
    "SUBMITTED_UNVERIFIED",
    "BLOCKED",
    "FAILED",
    "OUTCOME_UNKNOWN",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _masked_chat_id(chat_id: str) -> str:
    return f"***{chat_id[-4:]}" if len(chat_id) > 4 else "***"


def notification_statuses(config: dict[str, Any]) -> set[str]:
    settings = config.get("max_notifications") or {}
    if not isinstance(settings, dict):
        return set(DEFAULT_STATUSES)
    configured = settings.get("statuses")
    if not configured or isinstance(configured, (str, bytes)):
        return set(DEFAULT_STATUSES)
    return {str(status).strip().upper() for status in configured if str(status).strip()}


def format_exchange_message(payload: dict[str, Any]) -> str:
    """Create a compact human-readable message without secrets or traceback."""
    status = str(payload.get("status", "UNKNOWN"))
    labels = {
        "SUBMITTED_UNVERIFIED": "ПЕРЕДАНО, НУЖНА ПРОВЕРКА",
        "SUCCESS_VERIFIED": "УСПЕШНО",
        "BLOCKED": "ЗАБЛОКИРОВАНО",
        "FAILED": "ОШИБКА",
        "OUTCOME_UNKNOWN": "РЕЗУЛЬТАТ НЕИЗВЕСТЕН",
    }
    lines = [
        f"Mashaa Tilda Sync v{payload.get('app_version', '?')}",
        "",
        f"Статус: {labels.get(status, status)}",
        f"Запуск: {payload.get('run_id', 'не определён')}",
        f"Режим: {payload.get('mode', 'не определён')}",
        f"Склад: {payload.get('store', 'не определён')}",
        f"Этап: {payload.get('final_stage_index', 0)}/8 — {payload.get('final_stage', 'не определён')}",
        f"Сопоставлено: {payload.get('matched', 0)} из {payload.get('stock_rows', 0)}",
        f"Изменений: {payload.get('changes', 0)}",
        f"Длительность: {payload.get('duration_seconds', 0)} сек.",
    ]
    error = payload.get("error") or {}
    if error:
        lines.extend([
            f"Код: {error.get('code', 'E_UNKNOWN')}",
            f"Причина: {error.get('message', 'не определена')}",
            f"Действие: {error.get('action', 'проверить локальный error-лог')}",
        ])
    elif status == "SUBMITTED_UNVERIFIED":
        lines.append("Действие: проверить итоговые остатки в каталоге Tilda.")
    return redact_text("\n".join(lines))[:4000]


def _failed_outcome(payload: dict[str, Any], chat_id: str, exc: BaseException) -> dict[str, Any]:
    run_id = str(payload.get("run_id", "не определён"))
    message = redact_text(exc)
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logging.error("MAX: уведомление не отправлено | run_id=%s | причина=%s", run_id, message)
    logging.getLogger(ERROR_TRACE_LOGGER).error(
        "run_id=%s | Ошибка отправки уведомления в MAX chat_id=%s\n%s",
        run_id,
        _masked_chat_id(chat_id),
        redact_text(trace).rstrip(),
    )
    return {
        "enabled": True,
        "status": "failed",
        "channel": "group_chat",
        "chat_id_masked": _masked_chat_id(chat_id),
        "attempted_at": _utc_now(),
        "error": message,
    }


def notify_exchange(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Send one final exchange notification to a MAX group chat.

    All delivery errors are converted to a result object so MAX can never change
    the outcome of the stock exchange itself.
    """
    if not _enabled(os.getenv("MAX_NOTIFICATIONS_ENABLED")):
        return {"enabled": False, "status": "skipped", "reason": "disabled"}

    status = str(payload.get("status", "")).upper()
    if status not in notification_statuses(config):
        return {
            "enabled": True,
            "status": "skipped",
            "reason": "status_not_selected",
            "exchange_status": status,
        }

    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    chat_id = os.getenv("MAX_CHAT_ID", "").strip()
    if not token or not chat_id:
        missing = [name for name, value in (("MAX_BOT_TOKEN", token), ("MAX_CHAT_ID", chat_id)) if not value]
        return _failed_outcome(payload, chat_id, RuntimeError("Не заполнены переменные: " + ", ".join(missing)))
    try:
        int(chat_id)
    except ValueError:
        return _failed_outcome(payload, chat_id, RuntimeError("MAX_CHAT_ID должен быть числовым chat_id"))

    try:
        settings = config.get("max_notifications") or {}
        if not isinstance(settings, dict):
            raise ValueError("max_notifications должен быть JSON-объектом")
        api_base = str(settings.get("api_base", MAX_API_BASE)).rstrip("/")
        timeout = max(1, int(settings.get("timeout_seconds", 10)))
        query = urllib.parse.urlencode({"chat_id": chat_id})
        url = f"{api_base}/messages?{query}"
        body = json.dumps(
            {
                "text": format_exchange_message(payload),
                "notify": bool(settings.get("notify", True)),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": token,
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": f"mashaa-tilda-sync/{payload.get('app_version', '?')}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read()
            if response_body:
                json.loads(response_body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace")
        detail = detail.replace(token, "[REDACTED]")
        return _failed_outcome(payload, chat_id, RuntimeError(f"MAX HTTP {exc.code}: {detail}"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        return _failed_outcome(payload, chat_id, exc)

    logging.info("MAX: уведомление отправлено в групповой чат %s", _masked_chat_id(chat_id))
    return {
        "enabled": True,
        "status": "sent",
        "channel": "group_chat",
        "chat_id_masked": _masked_chat_id(chat_id),
        "sent_at": _utc_now(),
    }
