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
- At task start, use unresolved Project Board items supplied by `task_context` as advisory coordination context; Board availability or replies must never gate local work.
- Post a Project Board item when a blocker, open question, handoff, or cross-agent risk would help others align, but continue with the safest local path when the service is unavailable or nobody replies. Do not post routine progress updates.
- Query unresolved items when available to avoid duplicates. Reply on an existing thread when useful, and resolve after the outcome is locally observed or validated; never wait for a reply or remote confirmation solely to advance task state.
- Board identity and project membership come from the configured Hub token; do not put user or project identity in board content.