import os

import tomli as tomllib
from pathlib import Path

import tomli_w

from dto import AvitoConfig


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

    cfg = AvitoConfig(**data["avito"])

    _load_dotenv_simple(Path(path).resolve().parent)

    env_token = os.getenv("AVITO_TG_TOKEN") or os.getenv("TG_TOKEN")
    if env_token:
        cfg.tg_token = env_token

    return cfg


def save_avito_config(config: dict):
    with Path("config.toml").open("wb") as f:
        tomli_w.dump(config, f)
