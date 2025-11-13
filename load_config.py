import os
from pathlib import Path

import tomli as tomllib

import tomli_w

from dto import AvitoConfig, SearchQuery, RegionPreset, DeliveryMode

REGION_ALIASES: dict[str, RegionPreset] = {
    "all": "all",
    "все регионы": "all",
    "any": "all",
    "moscow": "moscow",
    "москва": "moscow",
    "moskva": "moscow",
    "moscow_only": "moscow",
    "mo": "mo",
    "московская область": "mo",
    "moscow_region": "mo",
    "moskovskaya_oblast": "mo",
    "moscow_and_mo": "moscow_mo",
    "moscow+mo": "moscow_mo",
    "moskva_i_mo": "moscow_mo",
    "москва и мо": "moscow_mo",
    "moscow_mo": "moscow_mo",
}

DELIVERY_ALIASES: dict[str, DeliveryMode] = {
    "any": "any",
    "все": "any",
    "delivery": "delivery_only",
    "delivery_only": "delivery_only",
    "with_delivery": "delivery_only",
    "доставка": "delivery_only",
    "pickup": "pickup_only",
    "no_delivery": "pickup_only",
    "pickup_only": "pickup_only",
    "без доставки": "pickup_only",
}


def _load_dotenv_simple(start_dir: Path | None = None):
    def parse_and_set(env_path: Path):
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
        except Exception:
            pass

    start = start_dir or Path.cwd()
    for parent in [start, *start.parents]:
        env_file = parent / ".env"
        if env_file.exists() and env_file.is_file():
            parse_and_set(env_file)
            break


def load_avito_config(path: str = "config.toml") -> AvitoConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    avito_section = data["avito"]
    avito_section = avito_section.copy()
    avito_section["searches"] = _parse_searches(avito_section)
    cfg = AvitoConfig(**avito_section)

    if not cfg.searches and cfg.queries:
        cfg.searches = [
            SearchQuery(
                text=query,
                region=_normalize_region(cfg.region_slug),
                min_price=cfg.min_price if cfg.min_price else None,
                max_price=cfg.max_price if cfg.max_price else None,
                delivery="delivery_only" if cfg.delivery_only else "any",
                sort_new=cfg.sort_new,
            )
            for query in cfg.queries
        ]

    _load_dotenv_simple(Path(path).resolve().parent)

    env_token = os.getenv("AVITO_TG_TOKEN") or os.getenv("TG_TOKEN")
    if env_token:
        cfg.tg_token = env_token

    return cfg


def save_avito_config(config: dict):
    with Path("config.toml").open("wb") as f:
        tomli_w.dump(config, f)


def _parse_searches(avito_section: dict) -> list[SearchQuery]:
    raw_searches = avito_section.get("searches") or []
    parsed: list[SearchQuery] = []
    for entry in raw_searches:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text") or entry.get("query")
        if not text:
            continue
        parsed.append(
            SearchQuery(
                text=str(text),
                region=_normalize_region(entry.get("region")),
                min_price=_to_int(entry.get("min_price")),
                max_price=_to_int(entry.get("max_price")),
                delivery=_normalize_delivery(entry.get("delivery")),
                sort_new=_to_bool(entry.get("sort_new")),
                track_price_changes=_to_bool(entry.get("track_price_changes"), default=True),
            )
        )
    return parsed


def _normalize_region(value) -> RegionPreset:
    if value is None:
        return "all"
    key = str(value).strip().lower()
    return REGION_ALIASES.get(key, "all")


def _normalize_delivery(value) -> DeliveryMode:
    if value is None:
        return "any"
    key = str(value).strip().lower()
    return DELIVERY_ALIASES.get(key, "any")


def _to_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        val = value.strip().lower()
        if val in {"1", "true", "yes", "on"}:
            return True
        if val in {"0", "false", "no", "off"}:
            return False
    return bool(value)
