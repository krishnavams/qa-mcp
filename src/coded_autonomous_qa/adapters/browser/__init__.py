"""Multi-engine browser automation: a common adapter interface, semantic targeting and an engine registry.

Optional engines are imported lazily, so only the engines a user installs are needed.
"""

from coded_autonomous_qa.adapters.browser.base import (
    AdapterError,
    BrowserAdapter,
    NavigationBlocked,
    StepOutcome,
)
from coded_autonomous_qa.adapters.browser.registry import (
    EngineError,
    EngineSpec,
    create_direct_adapter,
    engine_names,
    get_engine,
    register_engine,
    unregister_engine,
)
from coded_autonomous_qa.adapters.browser.semantic import (
    assertion_for_step,
    parse_assertion,
    target_for_step,
    to_description,
    to_playwright_selector,
)

__all__ = [
    "AdapterError",
    "BrowserAdapter",
    "EngineError",
    "EngineSpec",
    "NavigationBlocked",
    "StepOutcome",
    "assertion_for_step",
    "create_direct_adapter",
    "engine_names",
    "get_engine",
    "parse_assertion",
    "register_engine",
    "target_for_step",
    "to_description",
    "to_playwright_selector",
    "unregister_engine",
]
