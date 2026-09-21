"""Load and validate config.yaml (search criteria, sources, HTTP, notify, paths).

Pydantic models reject unknown keys and bad values, and load() turns the errors into a
short readable message. Secrets are not here: they come from .env (see .env.example).
Set HOUSEBOT_CONFIG to use a config file other than ./config.yaml.
"""

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PropertyType = Literal["house", "townhouse", "apartment", "vacant_land", "farm"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchConfig(Strict):
    province: str = "western-cape"
    towns: list[str] = []          # empty = any town
    exclude_towns: list[str] = []
    suburbs: list[str] = []
    exclude_suburbs: list[str] = []
    property_types: list[PropertyType] = ["house"]
    price_min: int | None = None
    price_max: int | None = None
    beds_min: float | None = None
    beds_max: float | None = None
    baths_min: float | None = None
    baths_max: float | None = None
    garages_min: int | None = None
    garages_max: int | None = None
    floor_min_m2: int | None = None
    floor_max_m2: int | None = None
    erf_min_m2: int | None = None
    erf_max_m2: int | None = None
    garden_min_m2: int | None = None  # estimated as erf - floor size (see match.py)
    include_keywords: list[str] = []
    exclude_keywords: list[str] = []
    include_auctions: bool = False
    unknown_values_pass: bool = True


class SourceConfig(Strict):
    enabled: bool = True
    max_pages: int = Field(15, ge=1, le=50)
    locations: dict[str, int] = {}  # town -> site location ID; empty = search the whole province
    province_id: int | None = None  # site ID of the province, used when locations is empty


class HttpConfig(Strict):
    min_delay_s: float = 5
    max_delay_s: float = 10
    timeout_s: float = 30
    user_agent: str = "housebot/1.0 (personal use)"

    @model_validator(mode="after")
    def _delays(self):
        if self.max_delay_s < self.min_delay_s:
            raise ValueError("max_delay_s must be >= min_delay_s")
        return self


class NotifyConfig(Strict):
    max_per_run: int = 25
    send_photo: bool = True
    quiet_if_none: bool = False


class CheckConfig(Strict):
    per_run: int = Field(10, ge=0)  # listing pages re-checked per daily run (favourites first)


class PathsConfig(Strict):
    db: Path = Path("data/housebot.db")


class Config(Strict):
    search: SearchConfig
    sources: dict[str, SourceConfig] = {}
    http: HttpConfig = HttpConfig()
    notify: NotifyConfig = NotifyConfig()
    check: CheckConfig = CheckConfig()
    paths: PathsConfig = PathsConfig()

    @model_validator(mode="after")
    def _sources_know_where_to_search(self):
        for name, src in self.sources.items():
            if not src.enabled:
                continue
            if not src.locations and src.province_id is None:
                raise ValueError(f"sources.{name}: set locations (town IDs) or province_id")
            missing = [t for t in self.search.towns if src.locations and t not in src.locations]
            if missing:
                raise ValueError(f"sources.{name}.locations has no ID for: {', '.join(missing)}")
        return self


class ConfigError(Exception):
    pass


def path() -> Path:
    """The config file in use: $HOUSEBOT_CONFIG, else ./config.yaml."""
    return Path(os.environ.get("HOUSEBOT_CONFIG", "config.yaml"))


def load(file: str | Path | None = None) -> Config:
    load_dotenv()
    path_ = Path(file or path())
    try:
        return Config.model_validate(yaml.safe_load(path_.read_text()) or {})
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path_}")
    except yaml.YAMLError as e:
        raise ConfigError(f"{path_} is not valid YAML: {e}")
    except ValidationError as e:
        lines = [f"  {'.'.join(map(str, err['loc'])) or '(root)'}: {err['msg']}" for err in e.errors()]
        raise ConfigError(f"Invalid config in {path_}:\n" + "\n".join(lines))
