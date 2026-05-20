from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class CompanyCfg:
    name: str
    start_date: str | None
    sources: list[str]
    seed_persons: str | None


@dataclass
class SourceCfg:
    code: str
    name: str
    base_url: str
    parser: str
    enabled: bool


@dataclass
class Config:
    start_date: str
    llm_provider: str
    llm_model: str
    output_root: Path
    db_path: Path
    auto_run: bool
    timezone: str
    companies: list[CompanyCfg] = field(default_factory=list)
    sources: dict[str, SourceCfg] = field(default_factory=dict)
    openai_api_key: str | None = None


def load_config(path: str | Path = "config.yaml") -> Config:
    load_dotenv(PROJECT_ROOT / ".env")

    cfg_path = Path(path)
    if not cfg_path.is_absolute():
        cfg_path = PROJECT_ROOT / cfg_path

    if not cfg_path.exists():
        example = PROJECT_ROOT / "config.example.yaml"
        raise FileNotFoundError(
            f"config not found: {cfg_path}\n"
            f"copy {example.name} to config.yaml and adjust"
        )

    with cfg_path.open(encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh)

    g = raw["global"]
    companies = [
        CompanyCfg(
            name=c["name"],
            start_date=c.get("start_date"),
            sources=list(c.get("sources", [])),
            seed_persons=c.get("seed_persons"),
        )
        for c in raw.get("companies", [])
    ]
    sources = {
        code: SourceCfg(
            code=code,
            name=s["name"],
            base_url=s["base_url"],
            parser=s["parser"],
            enabled=bool(s.get("enabled", False)),
        )
        for code, s in raw.get("sources", {}).items()
    }

    return Config(
        start_date=g["start_date"],
        llm_provider=g["llm_provider"],
        llm_model=g["llm_model"],
        output_root=(PROJECT_ROOT / g["output_root"]).resolve(),
        db_path=(PROJECT_ROOT / g["db_path"]).resolve(),
        auto_run=bool(g.get("auto_run", False)),
        timezone=g.get("timezone", "Europe/Moscow"),
        companies=companies,
        sources=sources,
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
    )
