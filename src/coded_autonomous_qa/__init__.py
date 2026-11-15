"""Coded Autonomous QA — a standalone MCP server.

The server is a *planner, validator and memory*, not a UI driver. UI work is
done by tools the MCP host (Coded) already exposes — PyMidsense vision tools,
the ``browser`` tool, the Automation Script Generator skill, the test runner —
so every engine returns **action requests** naming the host tool to call, and
the host reports what actually happened through the ``record_*`` / ``ingest_*``
tools. Nothing is reported as executed or verified until such a report exists.

Layers:

* ``config``      environment + Application Support data location
* ``safety``      URL/environment guard, permission tiers, approvals, masking, paths, secrets, audit
* ``knowledge``   pydantic models, repository interface and the SQLite implementation
* ``adapters``    host-tool catalog, git, POM index, skill, test results, read-only DB
* ``engines``     explorer (16), business rules (17), bug hunter (18), NL testing (19), regression (20)
* ``server``      MCP tools, resources and prompts
"""

__version__ = "0.1.0"
