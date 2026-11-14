import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, Niche
from app.db.session import make_engine


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch, request):
    """В обычных тестах Chromium не запускаем: браузерный слой проверяется в test_browser.py."""
    if "real_browser" in request.keywords:
        return
    from app.audit import runner
    from app.audit.browser import BrowserResult

    async def fake_capture(url, company_id):
        return BrowserResult()

    monkeypatch.setattr(runner, "capture", fake_capture)


@pytest.fixture
async def session_factory(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Niche(slug="gas", name="Газификация"))
        await s.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s
