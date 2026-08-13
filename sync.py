#!/usr/bin/env python3
"""MoySklad -> Tilda stock synchronizer.

Default behavior is read-only dry-run. A live CommerceML write requires both
--apply and an explicit confirmation phrase.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import http.cookiejar
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


APP_NAME = "mashaa-tilda-sync"
APP_VERSION = "0.4"
CONFIRM_PHRASE = "APPLY-TEST-TILDA"
TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class TildaItem:
    tilda_uid: str
    external_id: str
    title: str
    current_quantity: Decimal | None
    parent_uid: str
    editions: str


@dataclass(frozen=True)
class SourceStock:
    external_id: str
    name: str
    entity_type: str
    stock: Decimal
    reserve: Decimal
    in_transit: Decimal
    available: Decimal


def entity_key(meta: dict[str, Any] | None) -> str:
    """Return a stable MoySklad key independent of host, query and expansions."""
    meta = meta or {}
    href = str(meta.get("href", "")).strip()
    path_parts = [part for part in urllib.parse.urlparse(href).path.split("/") if part]
    entity_id = path_parts[-1] if path_parts else ""
    entity_type = ""
    if "entity" in path_parts:
        entity_index = len(path_parts) - 1 - path_parts[::-1].index("entity")
        if len(path_parts) > entity_index + 2:
            entity_type = path_parts[entity_index + 1]
            entity_id = path_parts[entity_index + 2]
    if not entity_type:
        entity_type = str(meta.get("type", "")).strip()
    if not entity_type and len(path_parts) >= 2:
        entity_type = path_parts[-2]
    if not entity_id:
        return ""
    return f"{entity_type}:{entity_id}" if entity_type else entity_id


@dataclass(frozen=True)
class Change:
    external_id: str
    tilda_uid: str
    title: str
    entity_type: str
    previous_quantity: Decimal | None
    source_stock: Decimal
    source_reserve: Decimal
    source_in_transit: Decimal
    new_quantity: Decimal


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SyncError(f"Invalid .env line {line_no}: expected NAME=value")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] == value[-1] and value[0] in {'\"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def require_env(name: str, *, needed: bool = True) -> str:
    value = os.getenv(name, "").strip()
    if needed and not value:
        raise SyncError(f"Missing environment variable: {name}")
    return value


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SyncError(f"Config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SyncError(f"Invalid JSON in {path}: {exc}") from exc
    required = [
        "moysklad_api_base",
        "store_name",
        "tilda_catalog_csv",
        "state_file",
        "log_dir",
        "output_dir",
    ]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise SyncError("Missing config keys: " + ", ".join(missing))
    return config


def setup_logging(log_dir: Path, verbose: bool) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"sync-{dt.datetime.now():%Y-%m-%d}.log"
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    return log_path


def decimal_or_none(value: Any) -> Decimal | None:
    text = "" if value is None else str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise SyncError(f"Invalid quantity value: {value!r}") from exc


def format_quantity(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def read_tilda_catalog(path: Path) -> list[TildaItem]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            required = {"Tilda UID", "External ID", "Title", "Quantity", "Parent UID", "Editions"}
            missing = required.difference(reader.fieldnames or [])
            if missing:
                raise SyncError("Tilda CSV missing columns: " + ", ".join(sorted(missing)))
            items = [
                TildaItem(
                    tilda_uid=(row["Tilda UID"] or "").strip(),
                    external_id=(row["External ID"] or "").strip(),
                    title=(row["Title"] or "").strip(),
                    current_quantity=decimal_or_none(row["Quantity"]),
                    parent_uid=(row["Parent UID"] or "").strip(),
                    editions=(row["Editions"] or "").strip(),
                )
                for row in reader
            ]
    except FileNotFoundError as exc:
        raise SyncError(f"Tilda catalog not found: {path}") from exc

    ids = [item.external_id for item in items if item.external_id]
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        raise SyncError(f"Duplicate External ID values in Tilda CSV: {duplicates[:10]}")
    missing_external = [item.tilda_uid for item in items if item.current_quantity is not None and not item.external_id]
    if missing_external:
        raise SyncError(f"Stock rows without External ID: {missing_external[:10]}")
    return items


class JsonHttpClient:
    def __init__(self, token: str, timeout: int, attempts: int, backoff: float):
        self.token = token
        self.timeout = timeout
        self.attempts = attempts
        self.backoff = backoff

    def get(self, url: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept-Encoding": "gzip",
            "Accept": "application/json;charset=utf-8",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        }
        request = urllib.request.Request(url, headers=headers, method="GET")
        for attempt in range(1, self.attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        import gzip
                        body = gzip.decompress(body)
                    return json.loads(body.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read(500).decode("utf-8", errors="replace")
                if exc.code not in TRANSIENT_HTTP_CODES or attempt == self.attempts:
                    raise SyncError(f"MoySklad HTTP {exc.code}: {detail}") from exc
                delay = self.backoff * (2 ** (attempt - 1))
                logging.warning("Transient MoySklad HTTP %s; retry in %.1fs", exc.code, delay)
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == self.attempts:
                    raise SyncError(f"MoySklad request failed: {exc}") from exc
                delay = self.backoff * (2 ** (attempt - 1))
                logging.warning("MoySklad request failed; retry in %.1fs", delay)
                time.sleep(delay)
        raise AssertionError("unreachable")


class MoySkladClient:
    def __init__(self, api_base: str, http: JsonHttpClient, page_size: int):
        self.api_base = api_base.rstrip("/")
        self.http = http
        self.page_size = min(max(int(page_size), 1), 1000)

    def paged_rows(self, endpoint: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        params = dict(params or {})
        while True:
            query = dict(params, limit=self.page_size, offset=offset)
            url = f"{self.api_base}/{endpoint.lstrip('/')}?{urllib.parse.urlencode(query)}"
            payload = self.http.get(url)
            page = payload.get("rows")
            if not isinstance(page, list):
                raise SyncError(f"Unexpected MoySklad response for {endpoint}: rows missing")
            rows.extend(page)
            total = int(payload.get("meta", {}).get("size", len(rows)))
            logging.debug("Fetched %s: %s/%s", endpoint, len(rows), total)
            if not page or len(rows) >= total or len(page) < self.page_size:
                break
            offset += len(page)
        return rows

    def resolve_store(self, store_name: str) -> dict[str, Any]:
        stores = self.paged_rows("entity/store")
        matches = [store for store in stores if str(store.get("name", "")).strip() == store_name]
        if len(matches) != 1:
            names = [str(store.get("name", "")) for store in stores]
            raise SyncError(f"Expected one store named {store_name!r}, found {len(matches)}. Available: {names}")
        return matches[0]

    def assortment_index(self) -> dict[str, dict[str, Any]]:
        rows = self.paged_rows("entity/assortment")
        index: dict[str, dict[str, Any]] = {}
        external_seen: set[str] = set()
        for row in rows:
            meta = row.get("meta") or {}
            href = str(meta.get("href", ""))
            stable_key = entity_key(meta)
            external = str(row.get("externalCode", "")).strip()
            if href:
                index[href] = row
            if stable_key:
                index[stable_key] = row
            if external:
                if external in external_seen:
                    raise SyncError(f"Duplicate externalCode in MoySklad assortment: {external}")
                external_seen.add(external)
        return index

    def stock_by_store(self, store: dict[str, Any]) -> dict[str, SourceStock]:
        assortment = self.assortment_index()
        rows = self.paged_rows("report/stock/bystore")
        store_href = str((store.get("meta") or {}).get("href", ""))
        store_id = store_href.rstrip("/").split("/")[-1]
        result: dict[str, SourceStock] = {}
        for row in rows:
            row_meta = row.get("meta") or {}
            href = str(row_meta.get("href", ""))
            stable_key = entity_key(row_meta)
            entity = assortment.get(href) or assortment.get(stable_key) or row
            external_id = str(entity.get("externalCode") or row.get("externalCode") or "").strip()
            if not external_id:
                continue
            selected: dict[str, Any] | None = None
            for stock_entry in row.get("stockByStore") or []:
                entry_meta = stock_entry.get("meta") or {}
                entry_href = str(entry_meta.get("href", ""))
                if entry_href == store_href or entry_href.rstrip("/").endswith("/" + store_id):
                    selected = stock_entry
                    break
            selected = selected or {}
            stock = decimal_or_none(selected.get("stock")) or Decimal("0")
            reserve = decimal_or_none(selected.get("reserve")) or Decimal("0")
            in_transit = decimal_or_none(selected.get("inTransit")) or Decimal("0")
            available = stock - reserve + in_transit
            entity_type = str((entity.get("meta") or {}).get("type") or row_meta.get("type") or "unknown")
            if external_id in result:
                raise SyncError(f"Duplicate stock externalCode in MoySklad: {external_id}")
            result[external_id] = SourceStock(
                external_id=external_id,
                name=str(entity.get("name") or row.get("name") or ""),
                entity_type=entity_type,
                stock=stock,
                reserve=reserve,
                in_transit=in_transit,
                available=available,
            )
        if rows and not result:
            sample_meta = rows[0].get("meta") or {}
            raise SyncError(
                "MoySklad stock rows could not be linked to assortment externalCode values; "
                f"sample stock key={entity_key(sample_meta)!r}, assortment index size={len(assortment)}"
            )
        return result


def read_state(path: Path, items: Iterable[TildaItem]) -> dict[str, Decimal]:
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            quantities = payload.get("quantities", {})
            return {str(key): Decimal(str(value)) for key, value in quantities.items()}
        except (json.JSONDecodeError, InvalidOperation, TypeError) as exc:
            raise SyncError(f"Invalid state file {path}: {exc}") from exc
    return {
        item.external_id: item.current_quantity
        for item in items
        if item.external_id and item.current_quantity is not None
    }


def write_state_atomic(path: Path, quantities: dict[str, Decimal]) -> None:
    payload = {
        "updated_at": utc_now(),
        "quantities": {key: format_quantity(value) for key, value in sorted(quantities.items())},
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def calculate_changes(
    items: list[TildaItem],
    source: dict[str, SourceStock],
    state: dict[str, Decimal],
    *,
    clamp_negative: bool,
    allow_fractional: bool,
    full: bool,
    minimum_match_ratio: float,
) -> tuple[list[Change], dict[str, Any]]:
    stock_items = [item for item in items if item.current_quantity is not None]
    matched = [item for item in stock_items if item.external_id in source]
    unmatched = [item for item in stock_items if item.external_id not in source]
    ratio = len(matched) / len(stock_items) if stock_items else 1.0
    if ratio < minimum_match_ratio:
        raise SyncError(
            f"Match ratio {ratio:.1%} is below required {minimum_match_ratio:.1%}; "
            f"matched {len(matched)} of {len(stock_items)}"
        )
    changes: list[Change] = []
    fractional: list[str] = []
    for item in matched:
        source_item = source[item.external_id]
        quantity = source_item.available
        if clamp_negative and quantity < 0:
            quantity = Decimal("0")
        if not allow_fractional and quantity != quantity.to_integral_value():
            fractional.append(item.external_id)
            continue
        previous = state.get(item.external_id, item.current_quantity)
        if full or previous != quantity:
            changes.append(
                Change(
                    external_id=item.external_id,
                    tilda_uid=item.tilda_uid,
                    title=item.title,
                    entity_type=source_item.entity_type,
                    previous_quantity=previous,
                    source_stock=source_item.stock,
                    source_reserve=source_item.reserve,
                    source_in_transit=source_item.in_transit,
                    new_quantity=quantity,
                )
            )
    if fractional:
        raise SyncError(f"Fractional quantities are not allowed: {fractional[:10]}")
    summary = {
        "tilda_rows": len(items),
        "stock_rows": len(stock_items),
        "matched": len(matched),
        "unmatched": len(unmatched),
        "unmatched_external_ids": [item.external_id for item in unmatched],
        "match_ratio": ratio,
        "changes": len(changes),
    }
    return changes, summary


def build_offers_xml(changes: list[Change], catalog_id: str, catalog_name: str) -> bytes:
    root = ET.Element(
        "КоммерческаяИнформация",
        {
            "ВерсияСхемы": "2.08",
            "ДатаФормирования": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    )
    package = ET.SubElement(root, "ПакетПредложений", {"СодержитТолькоИзменения": "true"})
    ET.SubElement(package, "Ид").text = catalog_id
    ET.SubElement(package, "Наименование").text = catalog_name
    ET.SubElement(package, "ИдКаталога").text = catalog_id
    ET.SubElement(package, "ИдКлассификатора").text = catalog_id
    offers = ET.SubElement(package, "Предложения")
    for change in changes:
        offer = ET.SubElement(offers, "Предложение")
        ET.SubElement(offer, "Ид").text = change.external_id
        ET.SubElement(offer, "Наименование").text = change.title
        ET.SubElement(offer, "Количество").text = format_quantity(change.new_quantity)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_import_stub_xml(catalog_id: str, catalog_name: str) -> bytes:
    """Build the non-destructive catalog file required before stock offers."""
    root = ET.Element(
        "КоммерческаяИнформация",
        {
            "ВерсияСхемы": "2.08",
            "ДатаФормирования": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    )
    classifier = ET.SubElement(root, "Классификатор")
    ET.SubElement(classifier, "Ид").text = catalog_id
    ET.SubElement(classifier, "Наименование").text = catalog_name
    ET.SubElement(classifier, "Группы")
    catalog = ET.SubElement(root, "Каталог", {"СодержитТолькоИзменения": "true"})
    ET.SubElement(catalog, "Ид").text = catalog_id
    ET.SubElement(catalog, "ИдКлассификатора").text = catalog_id
    ET.SubElement(catalog, "Наименование").text = catalog_name
    ET.SubElement(catalog, "Товары")
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def write_audit_csv(path: Path, changes: list[Change]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow([
            "External ID",
            "Tilda UID",
            "Title",
            "Entity Type",
            "Previous Quantity",
            "Stock",
            "Reserve",
            "In Transit",
            "New Quantity",
        ])
        for item in changes:
            writer.writerow([
                item.external_id,
                item.tilda_uid,
                item.title,
                item.entity_type,
                "" if item.previous_quantity is None else format_quantity(item.previous_quantity),
                format_quantity(item.source_stock),
                format_quantity(item.source_reserve),
                format_quantity(item.source_in_transit),
                format_quantity(item.new_quantity),
            ])
    temp.replace(path)


class CommerceMLClient:
    def __init__(self, url: str, username: str, password: str, timeout: int, attempts: int, backoff: float):
        self.base_url = url.rstrip("/") + "/"
        self.timeout = timeout
        self.attempts = attempts
        self.backoff = backoff
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.headers = {
            "Authorization": f"Basic {token}",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        }

    def _request(self, mode: str, *, method: str = "GET", data: bytes | None = None, filename: str | None = None) -> str:
        query: dict[str, str] = {"type": "catalog", "mode": mode}
        if filename:
            query["filename"] = filename
        url = self.base_url + "?" + urllib.parse.urlencode(query)
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
            headers["Content-Length"] = str(len(data))
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        for attempt in range(1, self.attempts + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8-sig", errors="replace").strip()
            except urllib.error.HTTPError as exc:
                detail = exc.read(500).decode("utf-8", errors="replace")
                if exc.code not in TRANSIENT_HTTP_CODES or attempt == self.attempts:
                    raise SyncError(f"Tilda CommerceML HTTP {exc.code}: {detail}") from exc
                delay = self.backoff * (2 ** (attempt - 1))
                logging.warning("Transient Tilda HTTP %s; retry in %.1fs", exc.code, delay)
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == self.attempts:
                    raise SyncError(f"Tilda CommerceML request failed: {exc}") from exc
                delay = self.backoff * (2 ** (attempt - 1))
                logging.warning("Tilda request failed; retry in %.1fs", delay)
                time.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _expect_success(response: str, operation: str) -> None:
        first_line = response.splitlines()[0].strip().lower() if response else ""
        if first_line not in {"success", "progress"}:
            raise SyncError(f"CommerceML {operation} failed: {response[:500]}")

    def _apply_checkauth_cookie(self, response: str) -> None:
        lines = [line.strip() for line in response.splitlines()]
        if len(lines) < 3 or lines[0].lower() != "success":
            raise SyncError("CommerceML checkauth did not return a session cookie")
        cookie_name = lines[1]
        cookie_value = lines[2]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", cookie_name):
            raise SyncError("CommerceML returned an invalid session cookie name")
        if not cookie_value or "\r" in cookie_value or "\n" in cookie_value or ";" in cookie_value:
            raise SyncError("CommerceML returned an invalid session cookie value")
        self.headers["Cookie"] = f"{cookie_name}={cookie_value}"

    @staticmethod
    def _log_status(operation: str, response: str) -> None:
        first_line = response.splitlines()[0].strip() if response else "empty"
        logging.info("CommerceML %s: %s", operation, first_line)

    def _poll_import(self, filename: str, transcript: list[tuple[str, str]]) -> None:
        for import_attempt in range(1, 21):
            result = self._request("import", filename=filename)
            operation = f"import-{filename}-{import_attempt}"
            transcript.append((operation, result))
            self._log_status(operation, result)
            self._expect_success(result, f"import {filename}")
            first_line = result.splitlines()[0].strip().lower() if result else ""
            if first_line == "success":
                return
            time.sleep(1)
        raise SyncError(f"CommerceML import of {filename} did not finish after 20 progress responses")

    def upload_offers(
        self,
        xml_bytes: bytes,
        catalog_id: str,
        catalog_name: str,
        import_filename: str = "import0_1.xml",
        offers_filename: str = "offers0_1.xml",
    ) -> list[tuple[str, str]]:
        transcript: list[tuple[str, str]] = []
        auth = self._request("checkauth")
        transcript.append(("checkauth", auth))
        self._expect_success(auth, "checkauth")
        self._apply_checkauth_cookie(auth)
        self._log_status("checkauth", auth)
        init = self._request("init")
        transcript.append(("init", init))
        self._log_status("init", init)
        if "zip=" not in init.lower() and not init.lower().startswith("success"):
            raise SyncError(f"CommerceML init failed: {init[:500]}")
        if re.search(r"(?im)^zip\s*=\s*yes\s*$", init):
            raise SyncError("Tilda requested ZIP CommerceML upload; version 0.1 supports only zip=no")
        import_bytes = build_import_stub_xml(catalog_id, catalog_name)
        for operation, filename, payload in (
            ("file-import", import_filename, import_bytes),
            ("file-offers", offers_filename, xml_bytes),
        ):
            upload = self._request("file", method="POST", data=payload, filename=filename)
            transcript.append((operation, upload))
            self._expect_success(upload, operation)
            self._log_status(operation, upload)
        self._poll_import(import_filename, transcript)
        self._poll_import(offers_filename, transcript)
        return transcript


def acquire_lock(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise SyncError(f"Another sync process appears to be running: {path}") from exc
    os.write(descriptor, f"pid={os.getpid()} started={utc_now()}\n".encode("utf-8"))
    return descriptor


def release_lock(path: Path, descriptor: int) -> None:
    try:
        os.close(descriptor)
    finally:
        path.unlink(missing_ok=True)


def run_once(base_dir: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    token = require_env("MOYSKLAD_TOKEN")
    http = JsonHttpClient(
        token,
        int(config.get("request_timeout_seconds", 30)),
        int(config.get("retry_attempts", 3)),
        float(config.get("retry_backoff_seconds", 2)),
    )
    ms = MoySkladClient(config["moysklad_api_base"], http, int(config.get("page_size", 1000)))
    tilda_csv = base_dir / config["tilda_catalog_csv"]
    state_path = base_dir / config["state_file"]
    items = read_tilda_catalog(tilda_csv)
    logging.info("Tilda snapshot loaded: %s rows", len(items))
    store = ms.resolve_store(config["store_name"])
    logging.info("MoySklad store resolved: %s", store.get("name"))
    source = ms.stock_by_store(store)
    logging.info("MoySklad stock entities loaded: %s", len(source))
    state = read_state(state_path, items)
    changes, summary = calculate_changes(
        items,
        source,
        state,
        clamp_negative=bool(config.get("clamp_negative_to_zero", True)),
        allow_fractional=bool(config.get("allow_fractional_quantity", False)),
        full=bool(args.full),
        minimum_match_ratio=float(config.get("minimum_match_ratio", 0.98)),
    )
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = base_dir / config["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_path = output_dir / f"audit-{timestamp}.csv"
    xml_path = output_dir / f"offers-{timestamp}.xml"
    summary_path = output_dir / f"summary-{timestamp}.json"
    write_audit_csv(audit_path, changes)
    xml_bytes = build_offers_xml(changes, config["commerce_catalog_id"], config["commerce_catalog_name"])
    xml_path.write_bytes(xml_bytes)
    result = dict(summary)
    result.update({
        "mode": "apply" if args.apply else "dry-run",
        "store": config["store_name"],
        "created_at": utc_now(),
        "audit_file": str(audit_path),
        "xml_file": str(xml_path),
        "applied": False,
    })
    if args.apply:
        if args.confirm != CONFIRM_PHRASE:
            raise SyncError(f"Live write requires --confirm {CONFIRM_PHRASE}")
        if not changes:
            logging.info("No stock changes to send")
        else:
            cml = CommerceMLClient(
                require_env("TILDA_CML_URL"),
                require_env("TILDA_CML_LOGIN"),
                require_env("TILDA_CML_PASSWORD"),
                int(config.get("request_timeout_seconds", 30)),
                int(config.get("retry_attempts", 3)),
                float(config.get("retry_backoff_seconds", 2)),
            )
            transcript = cml.upload_offers(
                xml_bytes,
                config["commerce_catalog_id"],
                config["commerce_catalog_name"],
            )
            result["commerce_transcript"] = [
                {"operation": operation, "response": response[:500]}
                for operation, response in transcript
            ]
            result["applied"] = True
            next_state = dict(state)
            for change in changes:
                next_state[change.external_id] = change.new_quantity
            write_state_atomic(state_path, next_state)
            logging.info("Applied %s stock changes to Tilda", len(changes))
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info(
        "Summary: matched=%s/%s, unmatched=%s, changes=%s, mode=%s",
        summary["matched"], summary["stock_rows"], summary["unmatched"], summary["changes"], result["mode"],
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync MoySklad available stock to Tilda via CommerceML")
    parser.add_argument("--config", default="config.json", help="Path to JSON config")
    parser.add_argument("--env", default=".env", help="Path to local env file")
    parser.add_argument("--apply", action="store_true", help="Write changes to Tilda; default is dry-run")
    parser.add_argument("--confirm", default="", help=f"Required with --apply: {CONFIRM_PHRASE}")
    parser.add_argument("--full", action="store_true", help="Send all matched stock rows, not only changes")
    parser.add_argument("--watch", action="store_true", help="Repeat continuously using configured interval")
    parser.add_argument("--verbose", action="store_true", help="Verbose diagnostic logs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    load_dotenv((base_dir / args.env).resolve() if not Path(args.env).is_absolute() else Path(args.env))
    config_path = (base_dir / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)
    try:
        config = load_config(config_path)
        setup_logging(base_dir / config["log_dir"], args.verbose)
        lock_path = base_dir / "sync.lock"
        lock_fd = acquire_lock(lock_path)
        try:
            if args.watch:
                interval = max(60, int(config.get("watch_interval_seconds", 60)))
                logging.info("Watch mode started; interval=%ss; apply=%s", interval, args.apply)
                while True:
                    started = time.monotonic()
                    try:
                        run_once(base_dir, config, args)
                    except SyncError as exc:
                        logging.error("Sync cycle failed: %s", exc)
                    elapsed = time.monotonic() - started
                    time.sleep(max(1, interval - elapsed))
            else:
                run_once(base_dir, config, args)
        finally:
            release_lock(lock_path, lock_fd)
        return 0
    except KeyboardInterrupt:
        logging.warning("Stopped by user")
        return 130
    except SyncError as exc:
        logging.error("%s", exc)
        return 2
    except Exception:
        logging.exception("Unexpected error")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
