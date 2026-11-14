"""Клиент Yandex Search API v2 (раздел 6.1).

По актуальной документации (aistudio.yandex.ru/docs/ru/search-api) доступен только API v2:
- синхронный  POST https://searchapi.api.cloud.yandex.net/v2/web/search      → {"rawData": base64}
- отложенный  POST https://searchapi.api.cloud.yandex.net/v2/web/searchAsync → Operation {id, done}
  результат:  GET  https://operation.api.cloud.yandex.net/operations/{id}    → {done, response.rawData}
Авторизация: `Authorization: Api-Key <ключ>` (роль сервисного аккаунта search-api.webSearch.user).

Режим `night_v1` из ТЗ реализован синхронным v2 в ночное окно (ночной тариф 00:00–07:59 МСК),
`deferred_v2` — отложенными запросами (доступны ночью, готовы через 5 мин – несколько часов,
хранятся 12 ч).
"""
from __future__ import annotations

import asyncio
import base64
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx
from loguru import logger

SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
SEARCH_ASYNC_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
OPERATION_URL = "https://operation.api.cloud.yandex.net/operations/{id}"

NO_RESULTS_CODE = "15"  # «Искомая комбинация слов нигде не встречается»


class YandexSearchError(Exception):
    """Ошибка Search API (HTTP или <error> в XML)."""


@dataclass
class SerpItem:
    url: str
    domain: str
    title: str
    position: int  # сквозная позиция в выдаче, с 1


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def parse_xml(xml: str, page: int = 0, per_page: int = 10) -> list[SerpItem]:
    """Разобрать XML-выдачу. Нет результатов → []. Ошибка API → YandexSearchError."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        logger.error("yandex: unparsable response: {}", xml[:2000])
        raise YandexSearchError(f"bad xml: {exc}") from exc
    err = root.find(".//error")
    if err is not None:
        code = err.get("code", "")
        if code == NO_RESULTS_CODE:
            return []
        raise YandexSearchError(f"code {code}: {_text(err)}")
    items: list[SerpItem] = []
    docs = root.findall(".//results//doc")
    if not docs and root.find(".//results") is None:
        logger.warning("yandex: no <results> in response: {}", xml[:2000])
    for i, doc in enumerate(docs):
        url = _text(doc.find("url"))
        if not url:
            continue
        items.append(
            SerpItem(
                url=url,
                domain=_text(doc.find("domain")),
                title=_text(doc.find("title")),
                position=page * per_page + i + 1,
            )
        )
    return items


class YandexSearchClient:
    """Минимальный клиент v2: синхронный поиск и отложенные операции."""

    def __init__(self, api_key: str, folder_id: str, region: int, per_page: int = 10, timeout: float = 60) -> None:
        if not api_key or not folder_id:
            raise YandexSearchError("YANDEX_SEARCH_API_KEY / YANDEX_FOLDER_ID не заданы")
        self.folder_id = folder_id
        self.region = region
        self.per_page = per_page
        self._client = httpx.AsyncClient(timeout=timeout, headers={"Authorization": f"Api-Key {api_key}"})

    async def aclose(self) -> None:
        await self._client.aclose()

    def _body(self, query: str, page: int) -> dict:
        return {
            "query": {
                "searchType": "SEARCH_TYPE_RU",
                "queryText": query[:400],
                "familyMode": "FAMILY_MODE_NONE",
                "page": str(page),
            },
            "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": str(self.per_page), "docsInGroup": "1"},
            "maxPassages": "1",
            "region": str(self.region),
            "l10n": "LOCALIZATION_RU",
            "folderId": self.folder_id,
            "responseFormat": "FORMAT_XML",
        }

    async def _post(self, url: str, body: dict) -> dict:
        for attempt in range(3):
            resp = await self._client.post(url, json=body)
            if resp.status_code == 429 or resp.status_code >= 500:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise YandexSearchError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        raise YandexSearchError(f"HTTP {resp.status_code} after retries")

    @staticmethod
    def _decode(raw: str) -> str:
        return base64.b64decode(raw).decode("utf-8", errors="replace")

    async def search(self, query: str, page: int = 0) -> list[SerpItem]:
        """Синхронный запрос одной страницы выдачи."""
        data = await self._post(SEARCH_URL, self._body(query, page))
        if "rawData" not in data:
            logger.error("yandex: unexpected sync response: {}", str(data)[:2000])
            raise YandexSearchError("no rawData in response")
        return parse_xml(self._decode(data["rawData"]), page, self.per_page)

    async def start_async(self, query: str, page: int = 0) -> str:
        """Создать отложенный запрос. Возвращает id операции."""
        data = await self._post(SEARCH_ASYNC_URL, self._body(query, page))
        if not data.get("id"):
            logger.error("yandex: unexpected async response: {}", str(data)[:2000])
            raise YandexSearchError("no operation id")
        return str(data["id"])

    async def get_operation(self, op_id: str, page: int = 0) -> list[SerpItem] | None:
        """Результат отложенного запроса: None — ещё не готов."""
        resp = await self._client.get(OPERATION_URL.format(id=op_id))
        if resp.status_code != 200:
            raise YandexSearchError(f"operation HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        if not data.get("done"):
            return None
        if "error" in data:
            raise YandexSearchError(f"operation error: {str(data['error'])[:500]}")
        raw = (data.get("response") or {}).get("rawData")
        if not raw:
            logger.error("yandex: finished operation without rawData: {}", str(data)[:2000])
            raise YandexSearchError("no rawData in operation")
        return parse_xml(self._decode(raw), page, self.per_page)
