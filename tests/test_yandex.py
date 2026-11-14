import base64
import json

import httpx
import pytest

from app.discovery import yandex_search as ys
from app.discovery.yandex_search import YandexSearchClient, YandexSearchError, parse_xml

XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0"><request/><response date="20261114T000000">
<found priority="all">1234</found>
<results><grouping attr="d" mode="deep" groups-on-page="10">
<page first="1" last="3">0</page>
<group><categ attr="d" name="gazsever.com"/><doc id="1">
<url>https://gazsever.com/</url><domain>gazsever.com</domain>
<title>Автономная <hlword>газификация</hlword> в СПб</title>
<passages><passage>текст</passage></passages></doc></group>
<group><doc id="2"><url>https://spb.avito.ru/uslugi/gaz</url><domain>spb.avito.ru</domain><title>Авито</title></doc></group>
<group><doc id="3"><url>http://gaz-proekt-spb.ru/uslugi/</url><domain>gaz-proekt-spb.ru</domain><title>Газ Проект</title></doc></group>
</grouping></results></response></yandexsearch>"""

NO_RESULTS = """<yandexsearch><response><error code="15">Искомая комбинация слов нигде не встречается</error></response></yandexsearch>"""
API_ERROR = """<yandexsearch><response><error code="32">Лимит запросов исчерпан</error></response></yandexsearch>"""


def test_parse_results():
    items = parse_xml(XML, page=1, per_page=10)
    assert [i.url for i in items] == ["https://gazsever.com/", "https://spb.avito.ru/uslugi/gaz", "http://gaz-proekt-spb.ru/uslugi/"]
    assert items[0].title == "Автономная газификация в СПб"
    assert [i.position for i in items] == [11, 12, 13]


def test_parse_errors():
    assert parse_xml(NO_RESULTS) == []
    with pytest.raises(YandexSearchError, match="32"):
        parse_xml(API_ERROR)
    with pytest.raises(YandexSearchError):
        parse_xml("<html>oops")


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


async def test_client_sync_async_and_auth():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/v2/web/search":
            return httpx.Response(200, json={"rawData": b64(XML)})
        if req.url.path == "/v2/web/searchAsync":
            return httpx.Response(200, json={"id": "op123", "done": False})
        if req.url.path == "/operations/op123":
            return httpx.Response(200, json={"done": True, "response": {"rawData": b64(XML)}})
        if req.url.path == "/operations/pending":
            return httpx.Response(200, json={"done": False, "id": "pending"})
        return httpx.Response(404)

    c = YandexSearchClient("KEY", "FOLDER", 2)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Authorization": "Api-Key KEY"})
    items = await c.search("газификация", page=0)
    assert len(items) == 3
    body = json.loads(seen[0].content)
    assert body["folderId"] == "FOLDER" and body["region"] == "2" and body["query"]["queryText"] == "газификация"
    assert body["responseFormat"] == "FORMAT_XML"
    assert seen[0].headers["Authorization"] == "Api-Key KEY"
    assert await c.start_async("газ", 1) == "op123"
    assert json.loads(seen[1].content)["query"]["page"] == "1"
    assert len(await c.get_operation("op123")) == 3
    assert await c.get_operation("pending") is None
    await c.aclose()


async def test_client_403():
    c = YandexSearchClient("KEY", "FOLDER", 2)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"code": 7, "message": "Permission denied"})))
    with pytest.raises(YandexSearchError, match="HTTP 403"):
        await c.search("x")


def test_client_requires_keys():
    with pytest.raises(YandexSearchError):
        YandexSearchClient("", "", 2)


def test_urls():
    assert ys.SEARCH_URL.endswith("/v2/web/search")
    assert ys.OPERATION_URL.format(id="x") == "https://operation.api.cloud.yandex.net/operations/x"
