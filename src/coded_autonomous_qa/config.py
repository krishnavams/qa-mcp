"""Environment-based configuration.

All settings come from ``CODED_QA_*`` environment variables, which is what an
MCP client's ``env`` block sets. Runtime data (knowledge base, evidence,
reports, audit log) lives **outside the repository**:

* macOS: ``~/Library/Application Support/coded-autonomous-qa``
* other: ``$XDG_DATA_HOME/coded-autonomous-qa`` (``~/.local/share/...``)

``CODED_QA_DATA_DIR`` overrides both.
"""

from __future__ import annotations

import os
import shlex
import sys
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

ENV_PREFIX = "CODED_QA_"
APP_DIR_NAME = "coded-autonomous-qa"

#: Environments in which the server will plan or accept UI execution.
TEST_ENVIRONMENTS = frozenset({"local", "dev", "test", "qa", "sit", "uat", "staging", "preprod-test"})

DEFAULT_DESTRUCTIVE_KEYWORDS = (
    "delete",
    "remove",
    "drop",
    "truncate",
    "purge",
    "destroy",
    "terminate",
    "wipe",
    "pay",
    "payment",
    "transfer",
    "purchase",
    "checkout",
    "place order",
    "buy now",
    "withdraw",
    "disburse",
    "send money",
    "wire",
    "refund",
    "close account",
    "deactivate",
    "cancel account",
    "reset password",
    "revoke",
    "approve loan",
    "sanction",
    "submit application",
)


class PermissionMode(StrEnum):
    """What the server may do. Discovery never touches the application."""

    DISCOVERY = "discovery"  # learn, analyse, plan, record host observations
    EXECUTION = "execution"  # additionally hand out execution action requests


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(ENV_PREFIX + name)
    return value if value not in (None, "") else default


def _env_list(name: str) -> list[str]:
    value = _env(name)
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    data_dir: Path
    environment: str = "local"
    permission_mode: PermissionMode = PermissionMode.DISCOVERY

    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1"],
        description="Hosts (or '*.suffix') an application URL may use.",
    )
    blocked_host_markers: list[str] = Field(
        default_factory=lambda: ["prod", "production", "live", "www."],
        description="Host substrings that mark production; always refused.",
    )
    allowed_roots: list[Path] = Field(
        default_factory=list, description="Directories the server may read (repos, POMs, results, evidence)."
    )
    destructive_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_DESTRUCTIVE_KEYWORDS))
    approval_ttl_minutes: int = 120

    host_profile: str = Field(default="coded", description="Name or path of the host tool profile.")
    pom_dirs: list[Path] = Field(default_factory=list)
    test_dirs: list[Path] = Field(default_factory=list)
    test_runner_cmd: list[str] = Field(default_factory=lambda: ["python", "-m", "pytest"])
    script_generator_skill: str = "automation-script-generator"
    pom_generator_skill: str = "automation-script-generator"

    app_db_paths: list[Path] = Field(default_factory=list, description="SQLite files opened read-only.")
    allow_secret_injection: bool = False
    max_screens_per_run: int = 40
    log_level: str = "INFO"

    @field_validator("environment")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()

    # derived locations -------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "knowledge.db"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def audit_log_path(self) -> Path:
        return self.data_dir / "logs" / "audit.jsonl"

    @property
    def is_test_environment(self) -> bool:
        return self.environment in TEST_ENVIRONMENTS

    def readable_roots(self) -> list[Path]:
        """Every directory the server may read: configured roots plus its own data dir."""
        return [*self.allowed_roots, self.data_dir]

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.evidence_dir, self.reports_dir, self.results_dir, self.audit_log_path.parent):
            d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:  # pragma: no cover - best effort on odd filesystems
            pass


def _paths(values: list[str]) -> list[Path]:
    return [Path(v).expanduser().resolve() for v in values]


def load_settings() -> Settings:
    kwargs: dict = {
        "data_dir": Path(_env("DATA_DIR") or default_data_dir()).expanduser().resolve(),
        "environment": _env("ENVIRONMENT", "local"),
        "permission_mode": PermissionMode(_env("PERMISSION_MODE", "discovery")),
        "allowed_roots": _paths(_env_list("ALLOWED_ROOTS")),
        "pom_dirs": _paths(_env_list("POM_DIRS")),
        "test_dirs": _paths(_env_list("TEST_DIRS")),
        "app_db_paths": _paths(_env_list("APP_DB_PATHS")),
        "host_profile": _env("HOST_PROFILE", "coded"),
        "allow_secret_injection": _env_bool("ALLOW_SECRET_INJECTION", False),
        "log_level": _env("LOG_LEVEL", "INFO"),
        "approval_ttl_minutes": int(_env("APPROVAL_TTL_MINUTES", "120")),
        "max_screens_per_run": int(_env("MAX_SCREENS_PER_RUN", "40")),
    }
    if hosts := _env_list("ALLOWED_HOSTS"):
        kwargs["allowed_hosts"] = [h.lower() for h in hosts]
    if markers := _env_list("BLOCKED_HOST_MARKERS"):
        kwargs["blocked_host_markers"] = [m.lower() for m in markers]
    if extra := _env_list("DESTRUCTIVE_KEYWORDS"):
        kwargs["destructive_keywords"] = [*DEFAULT_DESTRUCTIVE_KEYWORDS, *(k.lower() for k in extra)]
    if runner := _env("TEST_RUNNER_CMD"):
        kwargs["test_runner_cmd"] = shlex.split(runner)
    if skill := _env("SCRIPT_GENERATOR_SKILL"):
        kwargs["script_generator_skill"] = skill
    if skill := _env("POM_GENERATOR_SKILL"):
        kwargs["pom_generator_skill"] = skill
    return Settings(**kwargs)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
