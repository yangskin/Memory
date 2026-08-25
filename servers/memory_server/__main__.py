"""`python -m servers.memory_server` 的进程入口。

依赖守卫必须跑在 `from .server import main` **之前**：`server.py` 在模块顶层就
`from mcp.server import Server`，venv 里缺 `mcp`、过旧或装成未迁移的 2.x 时，进程会在
import 阶段直接抛异常退出，客户端只看到"Memory MCP 不可用"，既拿不到原因也没有修复
入口。

`ensure_ready()` 会先体检、必要时离线优先自动修复；确实修不好时不返回，而是转由
`dependency_fallback` 起一个纯标准库的降级 server（暴露诊断与重试修复两个工具）并在
其结束后终止进程，因此下面的 import 根本不会执行到。
"""

from .dependency_guard import ensure_ready
from .server_descriptions import SERVER_NAME

ensure_ready(SERVER_NAME)

from .server import main  # noqa: E402 — 必须在守卫之后导入


if __name__ == "__main__":
    raise SystemExit(main())
