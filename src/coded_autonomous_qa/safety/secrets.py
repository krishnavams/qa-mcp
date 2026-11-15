"""Secret references.

Test data never contains passwords. It contains ``${secret:NAME}`` references.
The server stores and reports only the reference. A value is resolved only
when ``CODED_QA_ALLOW_SECRET_INJECTION=true`` *and* an execution request is
being built; it is resolved from the macOS Keychain (service
``coded-autonomous-qa``, account ``NAME``) or from ``CODED_QA_SECRET_<NAME>``.
Resolved values are never persisted, logged or audited.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Any

SECRET_REF = re.compile(r"\$\{secret:([A-Za-z0-9_]{1,64})\}")
KEYCHAIN_SERVICE = "coded-autonomous-qa"


class SecretUnavailable(Exception):
    pass


def secret_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(SECRET_REF.findall(value))
    if isinstance(value, dict):
        return set().union(*(secret_names(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(secret_names(v) for v in value)) if value else set()
    return set()


def _lookup(name: str) -> str:
    env = os.environ.get(f"CODED_QA_SECRET_{name}")
    if env:
        return env
    if sys.platform == "darwin" and shutil.which("security"):
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.rstrip("\n")
    raise SecretUnavailable(
        f"Secret {name!r} not found. Store it with: security add-generic-password -s {KEYCHAIN_SERVICE} "
        f"-a {name} -w   (or set CODED_QA_SECRET_{name})."
    )


def resolve_secrets(value: Any) -> Any:
    """Return ``value`` with every ``${secret:NAME}`` replaced. Raises SecretUnavailable."""
    if isinstance(value, str):
        return SECRET_REF.sub(lambda m: _lookup(m.group(1)), value)
    if isinstance(value, dict):
        return {k: resolve_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_secrets(v) for v in value]
    return value
