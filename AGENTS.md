# Memory Hub Development Rules

- Do not move or refactor existing directories.
- Put new public service code only under the repository-root `memory_hub/` directory.
- The local MCP must not depend on FastAPI, SQLAlchemy, PostgreSQL drivers, or LLM SDKs.
- Local Memory writes must never wait for the public service.
- Do not implement agent online state, heartbeats, locks, leases, or a strongly consistent task state machine.
- Tokens determine `user_id`, `project_id`, and permissions; request bodies do not determine identity.
- Do not send private content to a project brief for another user.
- Brief workers have no tools, code execution, project file access, or URL access.
- New functionality must include tests.