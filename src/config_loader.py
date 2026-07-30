"""Load and validate config.yaml."""

from __future__ import annotations
import os
from pathlib import Path
from pydantic import BaseModel, field_validator
import yaml


class MovieConfig(BaseModel):
    name: str = ""
    id: str | int | None = None


class CinemaConfig(BaseModel):
    location: str = ""
    location_id: str | int | None = None
    name: str = ""
    id: str | int | None = None


class LocationRef(BaseModel):
    """A cinema location listed inside a watch entry."""
    id: int
    name: str = ""


class WatchConfig(BaseModel):
    """One movie, tracked at one or more locations."""
    movie: MovieConfig = MovieConfig()
    locations: list[LocationRef] = []

    @field_validator("locations", mode="before")
    @classmethod
    def allow_bare_ids(cls, v):
        """Accept `locations: [1, 3]` as shorthand for a list of mappings."""
        if not isinstance(v, list):
            return v
        return [{"id": item} if isinstance(item, (int, str)) else item for item in v]


class Target(BaseModel):
    """A movie+location pair. Each target is tracked independently."""
    movie_id: int
    movie_name: str = ""
    location_id: int
    location_name: str = ""

    @property
    def key(self) -> str:
        """Stable identifier used to key persisted state."""
        return f"{self.movie_id}@{self.location_id}"

    @property
    def label(self) -> str:
        movie = self.movie_name or f"Movie {self.movie_id}"
        location = self.location_name or f"Location {self.location_id}"
        return f"{movie} @ {location}"


class MonitoringConfig(BaseModel):
    interval_seconds: int = 60
    max_duration_minutes: int = 0
    max_consecutive_errors: int = 10
    fallback_to_browser: bool = True

    @field_validator("interval_seconds")
    @classmethod
    def min_interval(cls, v: int) -> int:
        if v < 30:
            return 30
        return v


class DesktopNotifConfig(BaseModel):
    enabled: bool = False


class TelegramConfig(BaseModel):
    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""
    chat_ids: list[str] = []

    @field_validator("chat_id", mode="before")
    @classmethod
    def stringify_chat_id(cls, v):
        return "" if v is None else str(v)

    @field_validator("chat_ids", mode="before")
    @classmethod
    def stringify_chat_ids(cls, v):
        if v is None:
            return []
        if isinstance(v, (str, int)):
            return [str(v)]
        return [str(item) for item in v]

    def recipients(self) -> list[str]:
        """Every chat that should receive alerts, `chat_id` first, deduped."""
        out: list[str] = []
        for cid in [self.chat_id, *self.chat_ids]:
            cid = cid.strip()
            if cid and cid not in out:
                out.append(cid)
        return out


class NotificationsConfig(BaseModel):
    desktop: DesktopNotifConfig = DesktopNotifConfig()
    telegram: TelegramConfig = TelegramConfig()


class LoggingConfig(BaseModel):
    level: str = "INFO"


class Config(BaseModel):
    watches: list[WatchConfig] = []
    movie: MovieConfig = MovieConfig()
    cinema: CinemaConfig = CinemaConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    logging: LoggingConfig = LoggingConfig()

    def targets(self) -> list[Target]:
        """Flatten `watches` into movie+location pairs, in config order.

        Falls back to the legacy top-level `movie`/`cinema` pair when no
        `watches` are defined, so pre-existing configs keep working.
        """
        targets: list[Target] = []
        seen: set[str] = set()

        def add(movie_id, movie_name, location_id, location_name) -> None:
            try:
                target = Target(
                    movie_id=int(movie_id),
                    movie_name=movie_name or "",
                    location_id=int(location_id),
                    location_name=location_name or "",
                )
            except (TypeError, ValueError):
                return
            if target.key not in seen:
                seen.add(target.key)
                targets.append(target)

        for watch in self.watches:
            for location in watch.locations:
                add(watch.movie.id, watch.movie.name, location.id, location.name)

        if not targets and self.movie.id and self.cinema.location_id:
            add(self.movie.id, self.movie.name, self.cinema.location_id, self.cinema.location)

        return targets

    def legacy_target_key(self) -> str | None:
        """Key the single-target state file used before multi-target support."""
        if self.movie.id and self.cinema.location_id:
            try:
                return f"{int(self.movie.id)}@{int(self.cinema.location_id)}"
            except (TypeError, ValueError):
                return None
        return None


def _parse_watch_targets(raw: str) -> list[WatchConfig]:
    """Parse `WATCH_TARGETS=1716@1,1716@3` into watch entries."""
    by_movie: dict[int, WatchConfig] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        movie_part, _, location_part = chunk.partition("@")
        try:
            movie_id, location_id = int(movie_part), int(location_part)
        except ValueError:
            raise ValueError(
                f"Invalid WATCH_TARGETS entry {chunk!r} — expected MOVIE_ID@LOCATION_ID"
            ) from None
        watch = by_movie.setdefault(movie_id, WatchConfig(movie=MovieConfig(id=movie_id)))
        watch.locations.append(LocationRef(id=location_id))
    return list(by_movie.values())


def load_config(path: str | Path | None = None) -> Config:
    """Load config from YAML file with env var overrides for secrets."""
    if path is None:
        path = Path(__file__).parent.parent / "config.yaml"
    path = Path(path)

    if not path.exists():
        config = Config()
    else:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        config = Config(**data)

    # Environment variable overrides for secrets
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        config.notifications.telegram.bot_token = token
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if chat_id:
        config.notifications.telegram.chat_id = chat_id
    chat_ids = os.environ.get("TELEGRAM_CHAT_IDS")
    if chat_ids:
        config.notifications.telegram.chat_ids = [
            c.strip() for c in chat_ids.split(",") if c.strip()
        ]

    # Environment variable overrides for movie/location (for CI/cron, where
    # config.yaml is not checked in).
    movie_id = os.environ.get("MOVIE_ID")
    if movie_id:
        config.movie.id = int(movie_id)
    movie_name = os.environ.get("MOVIE_NAME")
    if movie_name:
        config.movie.name = movie_name
    location_id = os.environ.get("LOCATION_ID")
    if location_id:
        config.cinema.location_id = int(location_id)
    location_name = os.environ.get("LOCATION_NAME")
    if location_name:
        config.cinema.location = location_name

    # An explicit MOVIE_ID/LOCATION_ID has to win over any `watches` in the
    # file, or the override would be silently dropped whenever a config.yaml
    # is present alongside it.
    if (movie_id or location_id) and config.movie.id and config.cinema.location_id:
        config.watches = [
            WatchConfig(
                movie=config.movie,
                locations=[
                    LocationRef(id=int(config.cinema.location_id), name=config.cinema.location)
                ],
            )
        ]

    # LOCATION_IDS spreads the single MOVIE_ID across several locations;
    # WATCH_TARGETS defines arbitrary movie+location pairs and wins outright.
    location_ids = os.environ.get("LOCATION_IDS")
    if location_ids and config.movie.id:
        config.watches = [
            WatchConfig(
                movie=config.movie,
                locations=[
                    LocationRef(id=int(lid.strip()))
                    for lid in location_ids.split(",")
                    if lid.strip()
                ],
            )
        ]
    watch_targets = os.environ.get("WATCH_TARGETS")
    if watch_targets:
        config.watches = _parse_watch_targets(watch_targets)

    return config


def save_config(config: Config, path: str | Path | None = None) -> None:
    """Save config back to YAML."""
    if path is None:
        path = Path(__file__).parent.parent / "config.yaml"
    path = Path(path)

    data = config.model_dump()
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
