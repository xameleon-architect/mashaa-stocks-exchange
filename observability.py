"""Human-readable and machine-readable exchange observability."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ERROR_TRACE_LOGGER = "mashaa.error_trace"


FINAL_STATUS_MESSAGES = {
    "DRY_RUN_READY": "Расчёт выполнен. Изменения подготовлены, отправки в Tilda не было.",
    "NO_CHANGES": "Обмен не требуется: по текущему снимку изменений нет.",
    "SUBMITTED_UNVERIFIED": "Пакет принят Tilda, но остаток в каталоге автоматически не проверен.",
    "SUCCESS_VERIFIED": "Обмен завершён, итоговые остатки проверены.",
    "BLOCKED": "Обмен остановлен защитной проверкой до отправки данных.",
    "FAILED": "Обмен не завершён из-за ошибки.",
    "OUTCOME_UNKNOWN": "Данные могли быть отправлены, но результат обработки Tilda неизвестен.",
    "STOPPED": "Обмен остановлен пользователем.",
}


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    action: str
    blocked: bool = False


ERROR_RULES: tuple[tuple[str, ErrorInfo], ...] = (
    (r"missing environment variable|config not found|invalid json|missing config keys|invalid \.env", ErrorInfo("E_CONFIG", "Проверить .env и config.json.", True)),
    (r"continuous apply is disabled", ErrorInfo("E_MODE_BLOCKED", "Использовать одиночный запуск записи или dry-run watch.", True)),
    (r"live write requires", ErrorInfo("E_CONFIRMATION", "Повторить запуск с требуемой фразой подтверждения.", True)),
    (r"another sync process", ErrorInfo("E_LOCK", "Проверить, не запущен ли другой экземпляр; после аварийной остановки удалить устаревший sync.lock.", True)),
    (r"duplicate", ErrorInfo("E_DUPLICATE_ID", "Исправить повторяющиеся External ID/externalCode и повторить dry-run.", True)),
    (r"match ratio", ErrorInfo("E_MATCH_RATIO", "Проверить сопоставление External ID Tilda и externalCode МоегоСклада.", True)),
    (r"fractional quantities", ErrorInfo("E_FRACTIONAL", "Проверить дробные остатки в МоемСкладе.", True)),
    (r"expected one store named", ErrorInfo("E_STORE_NOT_FOUND", "Проверить точное название склада в config.json.", True)),
    (r"tilda catalog not found|tilda csv missing|stock rows without|changed item not found|parent item|product without external", ErrorInfo("E_TILDA_DATA", "Проверить актуальность и структуру CSV-каталога Tilda.", True)),
    (r"invalid quantity value", ErrorInfo("E_DATA", "Проверить формат количеств в исходных данных.", True)),
    (r"moysklad http (401|403)", ErrorInfo("E_MOYSKLAD_AUTH", "Проверить токен МоегоСклада.")),
    (r"moysklad", ErrorInfo("E_MOYSKLAD_API", "Проверить доступность МоегоСклада и повторить dry-run.")),
    (r"checkauth|session cookie", ErrorInfo("E_CML_AUTH", "Проверить логин и пароль CommerceML в Tilda и .env.")),
    (r"file-import|file-offers|upload", ErrorInfo("E_CML_UPLOAD", "Проверить коннектор Tilda и журнал CommerceML перед повтором.")),
    (r"import of|commerce.*import", ErrorInfo("E_CML_IMPORT", "Проверить историю обмена Tilda; не считать остатки подтверждёнными.")),
    (r"tilda commerceml http (401|403)", ErrorInfo("E_CML_AUTH", "Проверить реквизиты CommerceML.")),
    (r"tilda|commerceml", ErrorInfo("E_CML_TRANSPORT", "Проверить доступность коннектора Tilda и повторить безопасную диагностику.")),
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def redact_text(value: Any) -> str:
    text = str(value)
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/=._-]+", r"\1 [REDACTED]", text)
    text = re.sub(
        r"(?i)\b(authorization|cookie|token|password|пароль)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[REDACTED]",
        text,
    )
    return text


def log_error_trace(exc: BaseException, *, run_id: str, stage_index: int, stage: str) -> None:
    """Write a redacted full traceback to the dedicated error journal."""
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logging.getLogger(ERROR_TRACE_LOGGER).error(
        "run_id=%s | Этап %s/%s — %s\n%s",
        run_id,
        stage_index,
        ExchangeRun.total_steps,
        stage,
        redact_text(trace).rstrip(),
    )


def classify_error(exc: BaseException) -> ErrorInfo:
    explicit_code = getattr(exc, "code", None)
    explicit_action = getattr(exc, "action", None)
    explicit_blocked = bool(getattr(exc, "blocked", False))
    if explicit_code:
        return ErrorInfo(str(explicit_code), str(explicit_action or "Проверить технический лог."), explicit_blocked)
    message = str(exc).lower()
    for pattern, info in ERROR_RULES:
        if re.search(pattern, message):
            return info
    return ErrorInfo("E_SYNC", "Сохранить summary и технический лог для диагностики.")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


class ExchangeRun:
    """Record one exchange cycle with exactly one final outcome."""

    total_steps = 8

    def __init__(self, base_dir: Path, config: dict[str, Any], *, mode: str, app_version: str):
        started = dt.datetime.now(dt.timezone.utc)
        token = started.astimezone().strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{token}-{uuid.uuid4().hex[:4].upper()}"
        self.started_at = started.isoformat(timespec="seconds")
        self.started_monotonic = time.monotonic()
        self.mode = mode
        self.app_version = app_version
        self.store = str(config.get("store_name", "не определён"))
        self.output_dir = base_dir / str(config.get("output_dir", "output"))
        self.log_dir = base_dir / str(config.get("log_dir", "logs"))
        self.status_dir = base_dir / str(config.get("status_dir", "status"))
        self.summary_path = self.output_dir / f"summary-{self.run_id}.json"
        self.events_path = self.log_dir / f"events-{started.astimezone():%Y-%m-%d}.jsonl"
        self.latest_path = self.status_dir / "latest.json"
        self.stage_index = 0
        self.stage = "Запуск"
        self.finished = False
        self.final_payload: dict[str, Any] | None = None
        self.partial: dict[str, Any] = {
            "submitted": False,
            "verified": False,
            "changes": 0,
        }

    def start(self) -> None:
        logging.info("=" * 72)
        logging.info("ОБМЕН ОСТАТКАМИ #%s", self.run_id)
        logging.info("Версия: %s | Режим: %s | Склад: %s", self.app_version, self.mode, self.store)
        self.event("exchange_started", message="Запуск обмена")

    def begin_step(self, index: int, title: str) -> None:
        self.stage_index = index
        self.stage = title
        logging.info("[%s/%s] %s", index, self.total_steps, title)
        self.event("step_started", message=title)

    def end_step(self, status: str, message: str, **metrics: Any) -> None:
        marker = status.upper()
        log_method = logging.warning if marker == "WARN" else logging.error if marker == "ERROR" else logging.info
        log_method("[%s] %s", marker, message)
        self.partial.update(metrics)
        self.event("step_completed", status=marker, message=message, **metrics)

    def detail(self, message: str) -> None:
        logging.info("      %s", message)

    def event(self, event: str, *, status: str | None = None, message: str = "", **data: Any) -> None:
        payload = {
            "timestamp": utc_now(),
            "run_id": self.run_id,
            "app_version": self.app_version,
            "event": event,
            "status": status,
            "mode": self.mode,
            "store": self.store,
            "stage_index": self.stage_index,
            "stage": self.stage,
            "message": redact_text(message),
        }
        payload.update(data)
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logging.error("Не удалось записать журнал событий: %s", exc)

    def finish(self, status: str, result: dict[str, Any], message: str | None = None) -> dict[str, Any]:
        if self.finished:
            return dict(self.final_payload or result)
        self.finished = True
        finished_at = utc_now()
        duration = round(time.monotonic() - self.started_monotonic, 3)
        payload = dict(self.partial)
        payload.update(result)
        payload.update({
            "schema_version": 1,
            "app": "mashaa-tilda-sync",
            "app_version": self.app_version,
            "run_id": self.run_id,
            "status": status,
            "status_message": message or FINAL_STATUS_MESSAGES.get(status, status),
            "mode": self.mode,
            "store": self.store,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "duration_seconds": duration,
            "final_stage_index": self.stage_index,
            "final_stage": self.stage,
            "summary_file": str(self.summary_path),
            "events_file": str(self.events_path),
        })
        self.final_payload = dict(payload)
        self.event(
            "exchange_completed",
            status=status,
            message=payload["status_message"],
            duration_seconds=duration,
            matched=payload.get("matched"),
            stock_rows=payload.get("stock_rows"),
            changes=payload.get("changes", 0),
            submitted=bool(payload.get("submitted")),
            verified=bool(payload.get("verified")),
            error_code=(payload.get("error") or {}).get("code"),
        )
        self._persist(payload)
        log_method = logging.error if status in {"FAILED", "BLOCKED", "OUTCOME_UNKNOWN"} else logging.warning if status == "SUBMITTED_UNVERIFIED" else logging.info
        log_method("ИТОГ: %s", status)
        log_method("%s", payload["status_message"])
        logging.info("Длительность: %.3f сек. | Отчёт: %s", duration, self.summary_path)
        logging.info("=" * 72)
        return payload

    def fail(self, exc: BaseException, result: dict[str, Any] | None = None) -> dict[str, Any]:
        info = classify_error(exc)
        outcome_unknown = bool(getattr(exc, "outcome_unknown", False))
        status = "OUTCOME_UNKNOWN" if outcome_unknown else "BLOCKED" if info.blocked else "FAILED"
        message = redact_text(exc)
        payload = dict(result or {})
        payload["error"] = {
            "code": info.code,
            "message": message,
            "action": info.action,
        }
        logging.error("[ERROR] Этап %s/%s — %s", self.stage_index, self.total_steps, self.stage)
        logging.error("Код: %s | Причина: %s", info.code, message)
        logging.error("Действие: %s", info.action)
        log_error_trace(
            exc,
            run_id=self.run_id,
            stage_index=self.stage_index,
            stage=self.stage,
        )
        return self.finish(status, payload)

    def record_notification(self, notification: dict[str, Any]) -> dict[str, Any]:
        """Attach an auxiliary delivery result without changing exchange status."""
        if not self.final_payload:
            raise RuntimeError("Exchange must be finished before recording a notification")
        payload = dict(self.final_payload)
        payload["max_notification"] = dict(notification)
        self.final_payload = payload
        self.event(
            "max_notification",
            status=str(notification.get("status", "unknown")).upper(),
            message=str(notification.get("reason") or notification.get("error") or "MAX notification processed"),
            channel=notification.get("channel"),
            chat_id_masked=notification.get("chat_id_masked"),
        )
        self._persist(payload)
        return dict(payload)

    def _persist(self, payload: dict[str, Any]) -> None:
        for path in (self.summary_path, self.latest_path):
            try:
                write_json_atomic(path, payload)
            except OSError as exc:
                logging.error("Не удалось записать %s: %s", path, exc)
