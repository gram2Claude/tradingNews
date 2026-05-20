"""Tests for the reporter: slugs, frontmatter, file layout, idempotency."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest
from openpyxl import load_workbook

from src import db, reporter
from src.config import Config, CompanyCfg, SourceCfg


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    seed = tmp_path / "seed.csv"
    seed.write_text(
        "full_name,status,brand\n"
        "Игорь Шехтерман,CEO,X5 Retail Group\n"
        "Ольга Наумова,руководитель бренда,Пятёрочка\n",
        encoding="utf-8",
    )
    return Config(
        start_date="2026-05-01",
        llm_provider="openai",
        llm_model="gpt-5-mini",
        output_root=tmp_path / "out",
        db_path=tmp_path / "db.sqlite",
        auto_run=False,
        timezone="Europe/Moscow",
        companies=[CompanyCfg("X5", None, ["x5_ir"], str(seed))],
        sources={"x5_ir": SourceCfg("x5_ir", "X5 IR", "https://www.x5.ru/", "x5_ir", True)},
        openai_api_key=None,
    )


def _insert_news(conn, *, headline, body, mood, published_utc, persons_full_names=()):
    cid = conn.execute("SELECT id FROM companies WHERE name='X5'").fetchone()["id"]
    sid = conn.execute("SELECT id FROM sources WHERE code='x5_ir'").fetchone()["id"]
    cur = conn.execute(
        "INSERT INTO news (company_id, source_id, url, headline, body, "
        "published_at, mood, mood_reason, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, sid, f"https://x.x/{headline[:30]}", headline, body,
         published_utc.isoformat(), mood, f"reason for {mood}", "analyzed"),
    )
    nid = cur.lastrowid
    for name in persons_full_names:
        pid = conn.execute(
            "SELECT id FROM persons WHERE full_name = ?", (name,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO news_persons (news_id, person_id) VALUES (?, ?)",
            (nid, pid),
        )
    return nid


def test_slug_basic() -> None:
    s = reporter.make_slug("Х5 объявляет о размере рекомендуемых дивидендов")
    assert s
    assert re.fullmatch(r"[а-яa-z0-9_]+", s)
    assert len(s) <= 60


def test_slug_first_five_words_only() -> None:
    s = reporter.make_slug("один два три четыре пять шесть семь восемь")
    # 6th word must not appear in slug
    assert "шесть" not in s
    assert "пять" in s


def test_slug_preserves_cyrillic_and_normalizes_yo() -> None:
    s = reporter.make_slug("Пятёрочка открыла магазин")
    assert s == "пятерочка_открыла_магазин"


def test_report_creates_md_persons_xlsx(cfg: Config) -> None:
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    _insert_news(
        conn,
        headline="Шехтерман объявил план развития",
        body="Подробный текст пресс-релиза.",
        mood="pos",
        published_utc=datetime(2026, 5, 19, 7, 0, tzinfo=timezone.utc),
        persons_full_names=["Игорь Шехтерман"],
    )
    _insert_news(
        conn,
        headline="Наумова прокомментировала ситуацию",
        body="Ещё один пресс-релиз.",
        mood="neg",
        published_utc=datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc),
        persons_full_names=["Ольга Наумова"],
    )
    conn.commit()
    conn.close()

    results = reporter.report_all(cfg, "X5")
    assert len(results) == 1
    r = results[0]
    assert r.md_files == 2
    assert r.xlsx_rows == 2
    assert r.xlsx_written is True
    assert r.persons_rows == 2

    # Files exist
    out = cfg.output_root / "X5"
    md_files = sorted((out / "news" / "2026" / "2026_05").glob("*.md"))
    assert len(md_files) == 2
    # Filename starts with date and has a 2-digit ordinal suffix
    for p in md_files:
        assert p.name.startswith("2026_05_19_")
        assert p.stem.endswith(("_01", "_02"))

    # MD content: every file has frontmatter + headline + persons block
    moods_found: set[str] = set()
    for p in md_files:
        text = p.read_text(encoding="utf-8")
        assert text.startswith("---")
        assert "persons: [" in text
        for m in ("pos", "neg", "neutral"):
            if f"mood: {m}" in text:
                moods_found.add(m)
    assert moods_found == {"pos", "neg"}

    # persons.csv: aggregate freqs reflect the data we inserted
    with (out / "affiliate" / "persons.csv").open(encoding="utf-8") as fh:
        rows = {r["person"]: r for r in csv.DictReader(fh)}
    assert rows["Игорь Шехтерман"]["pos_freq"] == "1"
    assert rows["Игорь Шехтерман"]["total_freq"] == "1"
    assert rows["Ольга Наумова"]["neg_freq"] == "1"

    # xlsx: 1 header + 2 rows
    wb = load_workbook(out / "news_list" / "data.xlsx")
    ws = wb.active
    assert ws.cell(1, 1).value == "date"
    assert ws.max_row == 3


def test_timezone_conversion_to_moscow(cfg: Config) -> None:
    """A UTC publication just before midnight on the last day of a month
    must land in the *next* month's folder when converted to Moscow time
    (UTC+3)."""
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    # 2026-05-31 22:00 UTC == 2026-06-01 01:00 Moscow
    _insert_news(
        conn,
        headline="Новость на границе месяца",
        body="Тест таймзоны.",
        mood="neutral",
        published_utc=datetime(2026, 5, 31, 22, 0, tzinfo=timezone.utc),
    )
    conn.commit()
    conn.close()

    reporter.report_all(cfg, "X5")
    files = list((cfg.output_root / "X5" / "news" / "2026" / "2026_06").glob("*.md"))
    assert len(files) == 1
    assert files[0].name.startswith("2026_06_01_")


def test_idempotent_regeneration(cfg: Config) -> None:
    db.init_db(cfg)
    conn = db.connect(cfg.db_path)
    _insert_news(
        conn,
        headline="Тест",
        body="Body",
        mood="pos",
        published_utc=datetime(2026, 5, 10, 9, tzinfo=timezone.utc),
    )
    conn.commit()
    conn.close()

    reporter.report_all(cfg, "X5")
    first = sorted((cfg.output_root / "X5" / "news").rglob("*.md"))
    reporter.report_all(cfg, "X5")
    second = sorted((cfg.output_root / "X5" / "news").rglob("*.md"))
    assert [p.name for p in first] == [p.name for p in second]


# Import here to keep top of file tidy
import re  # noqa: E402
