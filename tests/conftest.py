from __future__ import annotations

from pathlib import Path

import pytest

from coded_autonomous_qa.config import PermissionMode, Settings
from coded_autonomous_qa.knowledge import SQLiteKnowledgeRepository
from coded_autonomous_qa.knowledge.models import ControlObservation, FieldObservation, ScreenObservation
from coded_autonomous_qa.server import Services

BASE = "http://localhost:8080"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "project"
    ws.mkdir()
    return ws


def make_settings(tmp_path: Path, workspace: Path, mode: PermissionMode = PermissionMode.EXECUTION, **kw) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        environment="qa",
        permission_mode=mode,
        allowed_hosts=["localhost", "127.0.0.1", "*.qa.example.test"],
        allowed_roots=[workspace],
        **kw,
    )


@pytest.fixture
def settings(tmp_path: Path, workspace: Path) -> Settings:
    s = make_settings(tmp_path, workspace)
    s.ensure_dirs()
    return s


@pytest.fixture
def services(settings: Settings) -> Services:
    return Services(settings, SQLiteKnowledgeRepository(settings.db_path))


@pytest.fixture
def discovery_services(tmp_path: Path, workspace: Path) -> Services:
    s = make_settings(tmp_path, workspace, PermissionMode.DISCOVERY)
    s.ensure_dirs()
    return Services(s, SQLiteKnowledgeRepository(s.db_path))


def login_observation(**overrides) -> ScreenObservation:
    data = dict(
        url=f"{BASE}/login",
        title="Login",
        screen_name="Login",
        fields=[
            FieldObservation(
                name="username",
                label="Username",
                type="email",
                required=True,
                max_length=50,
                validation_messages=["Username is required"],
            ),
            FieldObservation(name="password", label="Password", type="password", required=True),
            FieldObservation(name="role", label="Role", type="select", options=["Customer", "Agent"]),
            FieldObservation(name="agent_code", label="Agent code", type="text", visible=False),
        ],
        controls=[
            ControlObservation(label="Sign in", kind="button"),
            ControlObservation(label="Forgot password", kind="link", href="/forgot"),
            ControlObservation(label="Delete account", kind="button"),
        ],
        messages=[],
        source_tool="browser+view_image",
    )
    data.update(overrides)
    return ScreenObservation(**data)


def forgot_observation() -> ScreenObservation:
    return ScreenObservation(
        url=f"{BASE}/forgot",
        title="Forgot password",
        screen_name="Forgot password",
        fields=[
            FieldObservation(
                name="email",
                label="Email",
                type="email",
                required=True,
                validation_messages=["Please enter a valid email"],
            )
        ],
        controls=[ControlObservation(label="Back to login", kind="link", href="/login")],
        source_tool="browser+view_image",
    )
