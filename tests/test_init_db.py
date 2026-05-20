"""Smoke test: init-db creates schema and loads seed."""
from __future__ import annotations

from pathlib import Path

import pytest

from src import db
from src.config import Config, CompanyCfg, SourceCfg


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    seed_csv = tmp_path / "x5.csv"
    seed_csv.write_text(
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
        companies=[
            CompanyCfg(
                name="X5",
                start_date=None,
                sources=["x5_ir"],
                seed_persons=str(seed_csv),
            )
        ],
        sources={
            "x5_ir": SourceCfg(
                code="x5_ir",
                name="X5 IR",
                base_url="https://www.x5.ru/ru/investors/",
                parser="x5_ir",
                enabled=True,
            )
        },
    )


def test_init_db_creates_tables_and_loads_seed(cfg: Config) -> None:
    counts = db.init_db(cfg)
    assert counts["companies"] == 1
    assert counts["sources"] == 1
    assert counts["persons"] == 2


def test_init_db_is_idempotent(cfg: Config) -> None:
    db.init_db(cfg)
    counts2 = db.init_db(cfg)
    assert counts2["persons"] == 2
