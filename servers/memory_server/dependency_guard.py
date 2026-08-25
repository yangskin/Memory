"""Memory MCP server 启动前的依赖体检与自动修复（纯标准库）。

为什么必须在 `import mcp` **之前**跑：`server.py` 在模块顶层就 `from mcp.server
import Server`。venv 里缺 `mcp`、或装成了未迁移的 2.x 时，进程在 import 阶段就抛
异常退出，客户端只能看到"Memory MCP 不可用"，既拿不到原因，也没有任何可触发的
修复入口。写在 server 内部的自检代码永远执行不到。

为什么不复用宿主仓库里同类的机制：本组件以 git subtree 形式独立发布，有自己的
requirements、vendor、CI 和消费方，不能 import 任何宿主仓库的代码，否则脱离那个仓库
就跑不起来。这里因此保留一份自包含实现，只依赖标准库。

安全边界（自动装包必须有边界）：

* 只在隔离环境（venv/virtualenv）里动手。解释器是系统 Python 或引擎自带 Python 时
  一律只诊断不安装，避免污染别的项目共享的解释器。
* 离线优先：先用 `vendor/` + `SHA256SUMS` 校验过的 wheel，失败才回退 PyPI。断网机器
  也能修，且优先装被锁定校验过的版本。
* pip 带超时，避免把客户端的 initialize 握手无限期挂住。
* 单进程只自动修一次，并按状态文件做冷却，避免客户端反复重启时陷入"修-崩-修"循环。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple, Optional

# .../MCP/Memory
MEMORY_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_REQUIREMENTS = MEMORY_ROOT / "requirements.txt"
VENDOR_DIR = MEMORY_ROOT / "vendor"
EXPECTED_VENV = MEMORY_ROOT / ".venv"

# 一次修复的墙钟上限。客户端普遍给 initialize 留 60s 量级；离线装一套 wheel 通常
# 10-20s，给到 180s 是为了慢盘/杀软扫描，同时仍然会失败而不是永久挂住。
OFFLINE_TIMEOUT_SEC = 180.0
ONLINE_TIMEOUT_SEC = 300.0

# `ensurepip` 只解包标准库自带的 wheel，不走网络，正常几秒内结束。
ENSUREPIP_TIMEOUT_SEC = 120.0

# "环境到底能不能用"的最终判据。必须是 server 真正会导入的东西：`mcp` 顶层包只是一个
# `__init__`，而 `mcp.server` 会把 fastmcp → pydantic-settings → python-dotenv 整条
# 依赖链都拉起来，装了一半的环境正是在那里炸的。
PROBE_MODULES = ("mcp.server",)

# 连续失败后的冷却时间：客户端会不停重启起不来的 server，没有冷却就会不停装包。
REPAIR_COOLDOWN_SEC = 600.0

# 同一进程树只允许自动修一次；子进程通过环境变量继承这个事实。
_ATTEMPT_ENV = "MEMORY_MCP_REPAIR_ATTEMPTED"

_STATE_FILENAME = ".memory_dependency_guard.json"
_LOCK_FILENAME = ".memory_dependency_guard.lock"

# 一次持锁修复的最坏耗时。这些超时是串行的：先恢复 pip（残留元数据让第一次 ensurepip
# 空转时会清理后再跑一次，所以是两次），然后离线装一遍、失败后联网装一遍；如果装完仍然
# import 不起来，还会带 `--force-reinstall` 把这两步再走一遍。
MAX_REPAIR_SEC = 2 * ENSUREPIP_TIMEOUT_SEC + 2 * (OFFLINE_TIMEOUT_SEC + ONLINE_TIMEOUT_SEC)

# 等修复锁的上限。必须覆盖对方一次完整的修复：提前放弃就意味着两个进程同时往一套
# site-packages 里跑 pip。
LOCK_WAIT_SEC = MAX_REPAIR_SEC + 60.0

REPAIR_HINT = (
    "run MCP/Memory/deploy.ps1 (offline-first: it installs from vendor/ with "
    "--no-index, then falls back to PyPI), or install into this component's own "
    "virtual environment with `pip install -r requirements.txt --no-index "
    "--find-links vendor`."
)

# 分发名在遇到这些字符时结束：版本约束、extras、environment marker、行内注释
_NAME_TERMINATORS = "=<>!~[;#, \t"


def _log(message: str) -> None:
    """写 stderr。

    stdout 是 MCP 的 JSON-RPC 通道，往里写任何东西都会破坏协议；stderr 会被客户端
    当作 server 日志展示，正是我们希望人和 LLM 看到修复过程的地方。
    """
    try:
        sys.stderr.write(f"[deps] {message}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — 日志失败绝不能影响启动
        pass


# ---------------------------------------------------------------- 体检


class Requirement(NamedTuple):
    """requirements 里一条能被体检的约束。"""

    name: str
    min_version: Optional[str] = None
    max_version: Optional[str] = None
    max_inclusive: bool = False

    def describe(self) -> Optional[str]:
        parts: list[str] = []
        if self.min_version:
            parts.append(f">={self.min_version}")
        if self.max_version:
            parts.append(f"{'<=' if self.max_inclusive else '<'}{self.max_version}")
        return ",".join(parts) if parts else None


def _read_version_at(text: str, start: int) -> Optional[str]:
    """从 start 处读一段版本号，遇到下一个分隔符或约束符停下。"""
    stop = len(text)
    for idx, ch in enumerate(text[start:]):
        if ch in ",;<>=! \t":
            stop = start + idx
            break
    candidate = text[start:stop].strip()
    return candidate or None


def _parse_requirement_line(line: str) -> Optional[Requirement]:
    """把一行 requirements 解析成 Requirement，无法解析时返回 None。

    只识别本组件 requirements 实际用到的形式（`name`、`name>=x.y`、`name>=x.y,<z`）。
    `-r` 递归、`-c` 约束、`--flag` 选项、URL/路径直装都跳过 —— 它们不是这里要体检的对象。

    上界必须解析：mcp 2.x 是 breaking rewrite，一台已经装了 2.x 的机器如果只比对
    下界，会被判成"依赖正常"，恰好漏掉最该抓的那种环境。
    """
    text = line.split("#", 1)[0].strip()
    if not text:
        return None
    if text.startswith("-") or "://" in text:
        return None

    cut = len(text)
    for idx, ch in enumerate(text):
        if ch in _NAME_TERMINATORS:
            cut = idx
            break
    name = text[:cut].strip()
    if not name:
        return None

    # environment marker 必须先切掉再找边界：`pkg>=1.0;python_version<3.12` 里的
    # `<3.12` 是 marker 的一部分，不是 pkg 的上界。留着它会把 3.12 当成 pkg 的上界，
    # 于是任何 4.x 的包都被误报成 incompatible。
    rest = text[cut:].split(";", 1)[0]

    min_version: Optional[str] = None
    marker = rest.find(">=")
    if marker != -1:
        min_version = _read_version_at(rest, marker + 2)

    max_version: Optional[str] = None
    max_inclusive = False
    for idx, ch in enumerate(rest):
        if ch != "<":
            continue
        # `<=` 是包含式上界，`<` 是排他上界；`>=` 里的 `=` 不会走到这里。
        if idx + 1 < len(rest) and rest[idx + 1] == "=":
            max_inclusive = True
            max_version = _read_version_at(rest, idx + 2)
        else:
            max_version = _read_version_at(rest, idx + 1)
        break

    return Requirement(name, min_version, max_version, max_inclusive)


# PEP 440 版本号。只用来排序，不做规范化输出。
_VERSION_RE = re.compile(
    r"^\s*v?"
    r"(?:(?P<epoch>\d+)!)?"
    r"(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre_l>alpha|beta|preview|pre|rc|a|b|c)[-_.]?(?P<pre_n>\d+)?)?"
    r"(?:(?:-(?P<post_n1>\d+))|(?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>\d+)?))?"
    r"(?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>\d+)?)?"
    r"(?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?"
    r"\s*$",
    re.IGNORECASE,
)

_PRE_RANKS = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}

# 排序哨兵：比任何真实 (rank, num) 都小 / 都大。
_NEG_SENTINEL = (-1, 0)
_INF_SENTINEL = (1 << 30, 0)


class Version(NamedTuple):
    """可比较的版本。`key` 用于排序，`base` 和 `is_prerelease` 用于边界规则。

    `base` 是 PEP 440 的 base version（epoch + release）。上界的预发布规则必须按它比较：
    只比 release 会让 `<1!2` 放过 `2.0rc1`（epoch 不同却被判成同一个 release）。
    """

    key: tuple
    base: tuple
    is_prerelease: bool


def _parse_version(version: str) -> Optional[Version]:
    """按 PEP 440 解析版本，无法识别时返回 None。

    以前这里只认纯数字点分版本，其余（`2.0.0rc1`、`1.29.0+local`、`1.26.0.post1`）
    一律返回 None，而调用方遇到 None 就跳过整条边界判断 —— 于是 `mcp>=1.27,<2` 对
    `2.0.0rc1` 会判成"依赖正常"，恰好漏掉最该抓的那种环境。
    """
    match = _VERSION_RE.match(version)
    if match is None:
        return None

    epoch = int(match.group("epoch") or 0)
    # 去掉末尾的 0，让 1.27 与 1.27.0 相等（PEP 440 语义），顺带解决段数不同的比较。
    release = tuple(int(part) for part in match.group("release").split("."))
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]

    pre_letter = match.group("pre_l")
    pre = (_PRE_RANKS[pre_letter.lower()], int(match.group("pre_n") or 0)) if pre_letter else None

    post_num = match.group("post_n1") or match.group("post_n2")
    post = (0, int(post_num)) if post_num is not None else (
        (0, 0) if match.group("post_l") else None
    )
    dev = (0, int(match.group("dev_n") or 0)) if match.group("dev_l") else None

    # 顺序：dev < pre < 正式版 < post
    if pre is None and post is None and dev is not None:
        pre_key = _NEG_SENTINEL
    elif pre is None:
        pre_key = _INF_SENTINEL
    else:
        pre_key = pre
    post_key = _NEG_SENTINEL if post is None else post
    dev_key = _INF_SENTINEL if dev is None else dev

    # local version（`1.0+abc`）参与排序：PEP 440 规定 `1.0+local > 1.0`。缺了它，
    # 等号包含的上界 `<=1.0` 会把 `1.0+local` 判成"没超"。有 local 时排在无 local 之后，
    # 具体标号之间的顺序不细分（本仓库不需要，也不值得再造一套 PEP 440 的分段比较）。
    local_key = 1 if match.group("local") else 0

    return Version(
        key=(epoch, release, pre_key, post_key, dev_key, local_key),
        base=(epoch, release),
        is_prerelease=pre is not None or dev is not None,
    )


def _exceeds_max(have: Version, want: Version, inclusive: bool) -> bool:
    """已安装版本是否越过了上界。"""
    if inclusive:
        return have.key > want.key
    if have.key >= want.key:
        return True
    # PEP 440：排他上界 `<V` 不允许 V 的预发布版，除非 V 本身就是预发布版。
    # 少了这条，`<2` 会放过 `2.0.0rc1` —— 它就是要挡的那个 breaking 大版本。
    if have.is_prerelease and not want.is_prerelease and have.base == want.base:
        return True
    return False


def check_dependencies(requirements_path: Optional[Path] = None) -> dict[str, Any]:
    """比对 requirements 与当前解释器已安装的分发，返回结构化体检结果。

    绝不抛异常：任何读取失败都降级为 `status="unknown"` 并带上原因，因为自检本身
    不该让 MCP server 起不来。

    版本判定是保守的：只有 requirements 写了边界、且双方都能按 PEP 440 解析时才比较，
    其余只报告已安装版本，不猜。
    """
    from importlib import metadata

    report: dict[str, Any] = {
        "status": "unknown",
        "requirements": None,
        "interpreter": sys.executable,
        "python_version": ".".join(str(p) for p in sys.version_info[:3]),
        "checked": 0,
        "missing": [],
        "outdated": [],
        "incompatible": [],
        "installed": {},
        "reason": None,
        "repair_hint": REPAIR_HINT,
    }

    path = Path(requirements_path) if requirements_path else DEFAULT_REQUIREMENTS
    report["requirements"] = str(path)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        report["reason"] = f"could not read requirements: {exc}"
        return report

    requirements: list[Requirement] = []
    for line in raw.splitlines():
        parsed = _parse_requirement_line(line)
        if parsed is not None:
            requirements.append(parsed)

    if not requirements:
        report["status"] = "ok"
        report["reason"] = "no pinned distributions found in requirements"
        return report

    for req in requirements:
        report["checked"] += 1
        required = req.describe()
        try:
            installed = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            report["missing"].append({"name": req.name, "required": required})
            continue
        except Exception as exc:  # noqa: BLE001 — 自检不能因元数据损坏而中断
            report["missing"].append({
                "name": req.name,
                "required": required,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        report["installed"][req.name] = installed
        have = _parse_version(installed)
        if have is None:
            continue

        want_min = _parse_version(req.min_version) if req.min_version else None
        if want_min is not None and have.key < want_min.key:
            report["outdated"].append({
                "name": req.name,
                "required": required,
                "installed": installed,
            })
            continue

        # 上界越界比过旧更危险：mcp 2.x 装上去这个 server 的行为无从保证，所以单列
        # 一类，不和 outdated 混在一起。
        want_max = _parse_version(req.max_version) if req.max_version else None
        if want_max is not None and _exceeds_max(have, want_max, req.max_inclusive):
            report["incompatible"].append({
                "name": req.name,
                "required": required,
                "installed": installed,
            })

    if report["missing"]:
        report["status"] = "missing"
    elif report["incompatible"]:
        report["status"] = "incompatible"
    elif report["outdated"]:
        report["status"] = "outdated"
    else:
        report["status"] = "ok"
    return report


def format_report(report: dict[str, Any]) -> str:
    """把体检结果压成一行人类可读文本，用于启动日志。"""
    status = report.get("status")
    if status == "ok":
        installed = report.get("installed") or {}
        summary = ", ".join(f"{k}=={v}" for k, v in sorted(installed.items()))
        return f"dependencies ok ({summary})" if summary else "dependencies ok"

    if status == "unknown":
        return f"dependency self-check skipped: {report.get('reason')}"

    problems: list[str] = []
    for item in report.get("missing") or []:
        problems.append(f"{item['name']} (missing, requires {item.get('required') or 'any'})")
    for item in report.get("incompatible") or []:
        problems.append(
            f"{item['name']} (installed {item['installed']} is OUTSIDE the "
            f"supported range {item.get('required')})"
        )
    for item in report.get("outdated") or []:
        problems.append(
            f"{item['name']} (installed {item['installed']}, requires {item.get('required')})"
        )
    return (
        f"dependency problems in {report.get('interpreter')}: "
        f"{'; '.join(problems)} - {report.get('repair_hint')}"
    )


# ---------------------------------------------------------------- 修复


def in_expected_venv() -> bool:
    """当前解释器是否就是本组件的 venv（`deploy.ps1` 造的那个）。"""
    try:
        return Path(sys.prefix).resolve() == EXPECTED_VENV.resolve()
    except OSError:
        return False


def is_isolated_env() -> bool:
    """当前解释器是否处在隔离环境（venv / virtualenv）里。

    这是允许自动装包的硬门槛。真正危险的是往共享解释器里装东西 —— 系统 Python 或
    UE 自带的 `Engine/Binaries/ThirdParty/Python3` 被改动会影响别的项目，而且用户
    根本没要求过。这两者都不是 venv，因此会被这条判断挡住。

    反过来，一个专门用来跑本 server 的 venv（哪怕不是 deploy 脚本建的那个）本来就是
    为此存在的，往里补齐声明过的依赖是修复而不是越权。
    """
    try:
        return sys.prefix != sys.base_prefix
    except AttributeError:  # 极老的解释器，保守起来当作非隔离
        return False


def state_path() -> Path:
    """修复状态文件位置。

    状态描述的是"这个解释器修过没有"，所以跟着解释器走，落在 `sys.prefix` 下而不是
    源码树里：venv 目录本身已被 `.gitignore` 忽略，不会给 subtree 带来脏文件。
    """
    return Path(sys.prefix) / _STATE_FILENAME


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    """原子写。

    直接 `write_text` 会先把文件截断再写：此刻进程被杀（客户端重启 server 时很常见）
    就留下一个空/半截的 JSON。`_read_state` 会把它当成"没有状态"，冷却随之失效，于是
    又开始"修-崩-修"。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        _log(f"could not persist repair state: {exc}")
        try:
            tmp.unlink()
        except (OSError, NameError, UnboundLocalError):
            pass


def lock_path() -> Path:
    """修复锁位置。与 state 同域：作用域就是这套 site-packages。"""
    return Path(sys.prefix) / _LOCK_FILENAME


def _try_lock_fd(fd: int) -> bool:
    """对已打开的 fd 尝试非阻塞独占锁。拿不到返回 False，不抛。"""
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_fd(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class _RepairLock:
    """`pip install` 的跨进程互斥锁（纯标准库，Windows/POSIX 通用）。

    Memory 的 venv 平时只有一个 server 在用，但客户端重启、手工 `--repair`、
    `Test-McpStartup.ps1` 都可能和它撞上。pip 自己没有跨进程锁：并发安装同一批 wheel
    会互相删改对方正在写的文件，把环境搞成半安装状态 —— 那正是本模块要修的那种坏环境。

    `_ATTEMPT_ENV` 挡不住这个：它只在单个进程树里生效。

    **锁由内核持有，不靠"锁文件是否存在"**（Windows 用 `msvcrt.locking`，POSIX 用
    `fcntl.flock`，都在标准库里）。这一点是正确性的关键而不是实现细节：以文件存在性
    表达持锁，就必须自己判定"持锁进程是不是已经死了"，而那个判断天生有竞态 ——
    两个等待者可以先后判定同一个锁陈旧，前者回收并成功持锁，后者随即把前者刚建好的锁
    当成陈旧的搬走，于是两个 pip 一起写同一套 site-packages（实测可复现）。改成内核锁
    后进程一死锁自动释放，既不需要陈旧阈值，也不需要给长安装续心跳。

    代价是锁文件本身不再删除（Windows 上删一个别人还开着的文件既会失败也会引入新竞态），
    所以文件存在不代表被持有。判断持有只能靠再抢一次。
    """

    # acquire() 的三种结局。区分后两者很关键：`timeout` 说明**别人正在装**，此时绝不能
    # 自己也去装；`unavailable` 说明这里根本建不了锁文件（只读目录之类），那是环境问题，
    # 不该因此让自动修复彻底失效。
    ACQUIRED = "acquired"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"

    _POLL_SEC = 0.25

    def __init__(self, path: Path, wait_sec: float) -> None:
        self.path = path
        self.wait_sec = wait_sec
        self.acquired = False
        self.waited_sec = 0.0
        self.contended = False
        self.status = self.UNAVAILABLE
        self._fd: Optional[int] = None

    def acquire(self) -> str:
        started = time.monotonic()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR)
        except OSError as exc:
            _log(f"could not open the repair lock, proceeding without one: {exc}")
            self.status = self.UNAVAILABLE
            return self.status

        self._fd = fd
        while True:
            if _try_lock_fd(fd):
                self.acquired = True
                self.waited_sec = time.monotonic() - started
                self.status = self.ACQUIRED
                return self.status

            self.contended = True
            self.waited_sec = time.monotonic() - started
            if self.waited_sec >= self.wait_sec:
                self.status = self.TIMEOUT
                self._close()
                return self.status
            time.sleep(self._POLL_SEC)

    def _close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    def release(self) -> None:
        if self._fd is not None and self.acquired:
            _unlock_fd(self._fd)
        self.acquired = False
        # 不删锁文件：内核锁已经释放，而在 Windows 上删除别人正开着的文件既会失败，
        # 也会让"删除与新持有者创建"之间出现新的竞态。留下一个空文件的代价可以忽略。
        self._close()


def _cooldown_remaining(state: dict[str, Any], now: float) -> float:
    """上次修复失败后还需等多久。返回 0 表示可以再试。"""
    if state.get("last_result") == "ok":
        return 0.0
    last = state.get("last_attempt_at")
    if not isinstance(last, (int, float)):
        return 0.0
    elapsed = now - float(last)
    if elapsed < 0:  # 系统时间被改过，别永久锁死
        return 0.0
    return max(0.0, REPAIR_COOLDOWN_SEC - elapsed)


def _pip_env() -> dict[str, str]:
    """给 pip 的环境变量。

    继承来的 `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` / `PIP_FIND_LINKS` 会让"离线优先"
    名不副实：`--no-index` 那一步本意是只用校验过的 vendor wheel，而这些变量能把它
    重新指向网络。`PIP_*` 一律剔除，让命令行参数成为唯一事实来源。

    同时固定 `PYTHONIOENCODING`：我们按 UTF-8 解码 pip 的输出，而 Windows 中文环境下
    子进程默认走 GBK，报错信息里的非 ASCII 字符会变成乱码，恰好是最需要看清的时候。
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _pip_is_available() -> bool:
    """当前解释器还能不能跑 pip。"""
    try:
        import importlib.util

        return importlib.util.find_spec("pip") is not None
    except Exception:  # noqa: BLE001 — 探测失败按"没有"处理，随后会尝试恢复
        return False


def _clear_stale_pip_metadata() -> list[str]:
    """删掉"包目录已不存在"的 pip 元数据，返回被删的目录名。

    只在已经确认 pip 不可导入之后调用。此时留下的 `pip-*.dist-info` 是纯粹的谎言，而它
    的破坏力很具体：`ensurepip` 内部是用捆绑的 pip 去装 pip 的，那个 pip 只看元数据判断
    "已满足"，于是报 `Requirement already satisfied: pip`、退出码 0、什么都没装。
    """
    removed: list[str] = []
    try:
        import sysconfig

        for key in ("purelib", "platlib"):
            root = sysconfig.get_paths().get(key)
            if not root or not Path(root).is_dir():
                continue
            if (Path(root) / "pip").is_dir():
                # 包目录还在，元数据不算残留 —— 不碰能用的安装。
                continue
            for pattern in ("pip-*.dist-info", "pip-*.egg-info"):
                for stale in Path(root).glob(pattern):
                    try:
                        shutil.rmtree(stale)
                        removed.append(stale.name)
                    except OSError as exc:
                        _log(f"could not remove stale {stale.name}: {exc}")
    except Exception as exc:  # noqa: BLE001 — 清理失败只该降级为诊断
        _log(f"could not scan for stale pip metadata: {exc}")
    return removed


def _run_ensurepip(timeout: float) -> tuple[bool, str]:
    """跑一次 `ensurepip --default-pip`。返回 (子进程是否成功, 诊断文本)。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ensurepip", "--default-pip"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=_pip_env(),
        )
    except subprocess.TimeoutExpired:
        return False, f"ensurepip timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, f"could not run ensurepip: {exc}"

    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
        return False, f"ensurepip exit={proc.returncode}: " + " | ".join(tail[-3:])

    _reset_metadata_caches()
    return True, "ensurepip completed"


def ensure_pip(timeout: float = ENSUREPIP_TIMEOUT_SEC) -> tuple[bool, str, bool]:
    """venv 里没有 pip 时用 `ensurepip` 把它装回来。

    这不是理论情况：删一个正在被 server 使用的 venv，在 Windows 上会因为打开着的
    `.pyd` 删到一半失败 —— pip 被删掉、几个 DLL 留下。这种 venv 里所有修复手段都会以
    `No module named pip` 收场，而那条消息完全没有指向"先把 pip 装回来"。`ensurepip`
    是标准库自带的 wheel，不需要网络，所以恢复它本身不依赖任何外部条件。

    返回 `(是否可用, 诊断文本, 是否动过环境)`。第三项让调用方区分"本来就有"和"我们修好了"，
    不用去猜诊断文本 —— 靠比较文案判断状态，改一次措辞就会把调用方悄悄改坏。
    """
    if _pip_is_available():
        return True, "pip already present", False
    if not is_isolated_env():
        # 共享解释器一律不动，理由和不往里装包一样。
        return False, f"pip is missing but {sys.executable} is not a virtual environment", False

    _log("pip is missing from this venv; restoring it with ensurepip")
    ok, note = _run_ensurepip(timeout)
    if not ok:
        return False, note, "could not run ensurepip" not in note
    if _pip_is_available():
        return True, "restored pip with ensurepip", True

    # 实测过的一步：`ensurepip` 退出码 0，pip 依然不可导入。原因是残留的
    # `pip-*.dist-info` —— 它内部那个 pip 只看元数据，于是报"已满足"、什么都不装。
    # 这正是这一层的"元数据齐全但装不起来"，处理方式也一样：先让元数据别再撒谎。
    stale = _clear_stale_pip_metadata()
    if not stale:
        return False, "ensurepip reported success but pip is still not importable", True

    _log(f"cleared stale pip metadata ({', '.join(stale)}); retrying ensurepip")
    ok, note = _run_ensurepip(timeout)
    if not ok:
        return False, f"{note} (after clearing {', '.join(stale)})", True
    if not _pip_is_available():
        return False, (
            "ensurepip reported success but pip is still not importable, even after "
            f"clearing stale metadata ({', '.join(stale)})"
        ), True
    return True, f"restored pip with ensurepip after clearing {', '.join(stale)}", True


def _run_pip(args: list[str], timeout: float) -> tuple[bool, str]:
    """跑一次 pip，返回 (是否成功, 诊断文本)。绝不抛异常。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            # pip 不需要也不应该继承 stdin：那是 MCP 的 JSON-RPC 通道。
            stdin=subprocess.DEVNULL,
            env=_pip_env(),
        )
    except subprocess.TimeoutExpired:
        return False, f"pip timed out after {timeout:.0f}s: {' '.join(args)}"
    except OSError as exc:
        return False, f"could not run pip: {exc}"

    if proc.returncode == 0:
        return True, "ok"
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    return False, f"pip exit={proc.returncode}: " + " | ".join(tail[-4:])


def verify_vendor(vendor_dir: Optional[Path] = None) -> tuple[bool, str]:
    """按 SHA256SUMS 校验离线 wheel 集。

    校验不通过就不用它：装来源不明的 wheel 比走网络更危险。
    """
    import hashlib

    vendor = Path(vendor_dir) if vendor_dir else VENDOR_DIR
    sums = vendor / "SHA256SUMS"
    if not vendor.is_dir():
        return False, "vendor/ is not present"
    if not sums.is_file():
        return False, "vendor/SHA256SUMS is missing"

    expected: dict[str, str] = {}
    try:
        for line in sums.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            parts = text.split(None, 1)
            if len(parts) != 2:
                return False, f"malformed SHA256SUMS line: {text[:60]}"
            name = parts[1].strip().lstrip("*")  # sha256sum 的二进制模式前缀
            # 清单条目必须是 vendor/ 里的纯文件名。`../` 或绝对路径会让校验读到
            # vendor 之外的文件，等于让清单自己决定校验哪个文件。
            if name != Path(name).name or name in {"", ".", ".."}:
                return False, f"SHA256SUMS entry is not a plain file name: {name[:60]}"
            expected[name] = parts[0].strip().lower()
    except OSError as exc:
        return False, f"could not read SHA256SUMS: {exc}"

    if not expected:
        return False, "SHA256SUMS lists no wheels"

    for name, digest in expected.items():
        path = vendor / name
        if not path.is_file():
            return False, f"{name} is listed but missing"
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            return False, f"could not hash {name}: {exc}"
        if actual != digest:
            return False, f"{name} does not match its recorded digest"

    # 多余 wheel 会让 --no-index 装到未记录的版本，同样按不可用处理。
    try:
        for path in vendor.glob("*.whl"):
            if path.name not in expected:
                return False, f"untracked wheel in vendor/: {path.name}"
    except OSError as exc:
        return False, f"could not list vendor/: {exc}"

    return True, f"verified {len(expected)} wheel(s)"


def repair(
    requirements: Optional[Path] = None,
    allow_network: bool = True,
    vendor_dir: Optional[Path] = None,
    force_reinstall: bool = False,
    verify_modules: tuple[str, ...] = (),
) -> dict[str, Any]:
    """尝试把当前 venv 修回 requirements 声明的状态。

    `force_reinstall=True` 用于"元数据齐全但 import 不起来"这一类环境：pip 只看
    `dist-info` 判断是否已满足，包目录被删掉、`dist-info` 还留着时，普通 `install -r`
    是个空操作，会一遍遍报成功而环境始终坏着。只在升级路径上用它，因为它会把
    requirements 里每个包都重装一次。

    `verify_modules` 非空时，装完还要真的 import 一遍；import 不起来就自动带
    `--force-reinstall` 再装一次。这一步放在 `repair()` 里而不是各调用方，是因为它必须
    发生在持锁期间：调用方各自"装完→验证→再装一次"的话，两次安装之间锁是放开的，别的
    进程正好能挤进来开第二个 pip。

    返回结构化结果，绝不抛异常 —— 修复失败必须变成可报告的信息，而不是新的崩溃。
    """
    req = Path(requirements) if requirements else DEFAULT_REQUIREMENTS
    vendor = Path(vendor_dir) if vendor_dir else VENDOR_DIR
    result: dict[str, Any] = {
        "attempted": False,
        "repaired": False,
        "method": None,
        "steps": [],
        "error": None,
        "requirements": str(req),
        "vendor": str(vendor),
        "interpreter": sys.executable,
    }

    if not is_isolated_env():
        result["error"] = (
            f"refusing to auto-install: {sys.executable} is not a virtual environment "
            "(installing here would modify a Python that other projects share). Run "
            f"MCP/Memory/deploy.ps1 and start the server with {EXPECTED_VENV}."
        )
        return result

    if not in_expected_venv():
        # 允许，但要说清我们在改哪个环境 —— 静默改一个非预期的 venv 同样难排查。
        _log(f"note: repairing {sys.prefix}, which is not {EXPECTED_VENV}")

    if not req.is_file():
        result["error"] = f"requirements file not found: {req}"
        return result

    result["attempted"] = True
    result["force_reinstall"] = force_reinstall

    # pip 自己可能就是缺的那一块。先补回来，否则后面每一步都只会报
    # `No module named pip`，而那条消息不会告诉任何人该怎么办。
    pip_ok, pip_note, pip_changed = ensure_pip()
    if pip_changed:
        result["steps"].append({"step": "ensure_pip", "ok": pip_ok, "detail": pip_note})
    if not pip_ok:
        result["error"] = f"pip is unavailable and could not be restored: {pip_note}"
        return result

    vendor_ok, vendor_note = verify_vendor(vendor)
    result["steps"].append({"step": "verify_vendor", "ok": vendor_ok, "detail": vendor_note})
    if not vendor_ok:
        _log(f"offline wheel set unusable ({vendor_note})")

    def install(force: bool) -> tuple[bool, Optional[str], Optional[str]]:
        """跑一轮"离线优先、失败回退 PyPI"的安装。返回 (是否成功, 方法, 错误)。"""
        extra = ["--force-reinstall"] if force else []
        tag = "_forced" if force else ""

        if vendor_ok:
            _log("repairing from verified offline wheels...")
            ok, detail = _run_pip(
                ["install", "-r", str(req), "--no-index", "--find-links", str(vendor), *extra],
                OFFLINE_TIMEOUT_SEC,
            )
            result["steps"].append(
                {"step": f"install_offline{tag}", "ok": ok, "detail": detail}
            )
            if ok:
                return True, "offline", None

        if not allow_network:
            return False, None, "offline repair failed and network fallback is disabled"

        _log("offline repair unavailable or failed; trying PyPI...")
        ok, detail = _run_pip(
            ["install", "-r", str(req), "--retries", "2", "--timeout", "60", *extra],
            ONLINE_TIMEOUT_SEC,
        )
        result["steps"].append({"step": f"install_online{tag}", "ok": ok, "detail": detail})
        if ok:
            return True, "online", None
        return False, None, "both offline and online repair failed; see steps for pip output"

    ok, method, error = install(force_reinstall)
    result["repaired"], result["method"], result["error"] = ok, method, error
    if not ok or not verify_modules:
        return result

    # 装完必须真的 import 一遍。元数据齐全不等于装得能用，而 pip 报成功也不等于装了东西：
    # 包目录被删掉、`dist-info` 还留着时，`install -r` 是个空操作。
    _activate_site_packages()
    _reset_metadata_caches()
    import_ok, import_error = probe_imports(verify_modules)
    result["steps"].append(
        {"step": "verify_imports", "ok": import_ok, "detail": import_error or "ok"}
    )
    if import_ok or force_reinstall:
        # 已经是强制重装那一轮了，再来一次也不会有别的结果。
        result["repaired"] = import_ok
        if not import_ok:
            result["error"] = f"installed but still not importable: {import_error}"
        return result

    _log(f"install left the environment unusable ({import_error}); "
         "retrying with --force-reinstall")
    result["force_reinstall"] = True
    ok, method, error = install(True)
    result["method"], result["error"] = method, error
    if not ok:
        result["repaired"] = False
        return result

    _activate_site_packages()
    _reset_metadata_caches()
    import_ok, import_error = probe_imports(verify_modules)
    result["steps"].append(
        {"step": "verify_imports_forced", "ok": import_ok, "detail": import_error or "ok"}
    )
    result["repaired"] = import_ok
    if not import_ok:
        result["error"] = f"force-reinstalled but still not importable: {import_error}"
    return result


def locked_repair(
    requirements: Optional[Path] = None,
    allow_network: bool = True,
    vendor_dir: Optional[Path] = None,
    lock_file: Optional[Path] = None,
    lock_wait_sec: Optional[float] = None,
    force_reinstall: bool = False,
    verify_modules: tuple[str, ...] = (),
) -> dict[str, Any]:
    """显式修复入口用的加锁包装：等到没人在装了，再自己装。

    显式修复（兜底 server 的 `memory_repair_environment`、`--repair` 命令行）刻意绕过
    冷却与"本进程树已试过"这两道闸门 —— 它们只为防止客户端自动重启导致的装包循环。
    但跨进程锁不在可绕过之列：绕过它就等于允许"一次人工重试"和"启动期自动修复"同时往
    同一套 site-packages 跑 pip，而那正是这把锁要防的半安装状态。

    与自动路径的差别只有一处：抢到锁后照样装一遍，不会因为"别人刚修好"就跳过 —— 显式
    重试是用户/LLM 主动要求的，pip 本身幂等，重装一次比让人怀疑命令没生效更好。
    """
    lock = _RepairLock(
        lock_file if lock_file is not None else lock_path(),
        wait_sec=LOCK_WAIT_SEC if lock_wait_sec is None else lock_wait_sec,
    )
    status = lock.acquire()
    lock_info = {
        "status": status,
        "acquired": lock.acquired,
        "waited_sec": round(lock.waited_sec, 2),
    }
    try:
        if status == _RepairLock.TIMEOUT:
            # 返回结构必须和 `repair()` 一致：调用方会遍历 steps、读 error。
            return {
                "attempted": False,
                "repaired": False,
                "method": None,
                "steps": [],
                "error": (
                    f"another process has been repairing this environment for "
                    f"{lock.waited_sec:.0f}s; refusing to start a second concurrent "
                    "install. Wait for it to finish, then try again."
                ),
                "requirements": str(Path(requirements) if requirements else DEFAULT_REQUIREMENTS),
                "vendor": str(Path(vendor_dir) if vendor_dir else VENDOR_DIR),
                "interpreter": sys.executable,
                "lock": lock_info,
            }

        result = repair(
            requirements=requirements,
            allow_network=allow_network,
            vendor_dir=vendor_dir,
            force_reinstall=force_reinstall,
            verify_modules=verify_modules,
        )
        result["lock"] = lock_info
        return result
    finally:
        lock.release()


def _reset_metadata_caches() -> None:
    """让 importlib.metadata 看见刚装的分发。

    pip 装完后 sys.path 上的目录内容变了，但 Python 缓存了目录列表；不失效缓存的话
    复检会读到旧状态，明明修好了却报告仍然缺失。
    """
    try:
        import importlib

        importlib.invalidate_caches()
    except Exception:  # noqa: BLE001
        pass


def _activate_site_packages() -> None:
    """让当前进程认到刚装的包里由 `.pth` 注入的路径。

    实测踩到的坑：`mcp` 在 win32 上依赖 pywin32，而 pywin32 把 `win32/lib` 等目录靠
    `pywin32.pth` 加进 sys.path。`.pth` 只在解释器启动时被 site 处理，所以刚装完
    pywin32 的这个进程仍然 `import pywintypes` 失败 —— 环境其实已经修好了，表现却和
    "没修好"完全一样，最难排查的一类假象。

    `site.addsitedir()` 会重新扫描该目录下的 `.pth` 并执行其中的路径与 import 行，
    等价于补做一次启动时的处理，因此不必换进程（换进程会让客户端持有的 PID 失效，
    stdio 通道也跟着断）。
    """
    try:
        import site
        import sysconfig

        paths = sysconfig.get_paths()
        for key in ("purelib", "platlib"):
            candidate = paths.get(key)
            if candidate and Path(candidate).is_dir():
                site.addsitedir(candidate)
    except Exception as exc:  # noqa: BLE001 — 补做 site 处理失败只该降级为诊断
        _log(f"could not re-process site-packages after repair: {exc}")


def probe_imports(modules: tuple[str, ...]) -> tuple[bool, Optional[str]]:
    """真的 import 一遍关键模块，作为"环境可用"的最终判据。

    元数据齐全并不等于装得能用（`.pth` 未生效、wheel 与解释器 ABI 不匹配、装了一半
    的分发都会这样）。守卫的承诺是"绝不让客户端看到一条没有解释的 import 崩溃"，所以
    放行之前必须自己先 import 一次；这几个模块紧接着就会被 server 导入，因此没有额外
    开销。
    """
    if not modules:
        return True, None

    import importlib

    for name in modules:
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 — 坏安装什么都可能抛
            return False, f"import {name} failed: {type(exc).__name__}: {exc}"
    return True, None


def ensure_dependencies(
    requirements: Optional[Path] = None,
    allow_repair: bool = True,
    allow_network: bool = True,
    state_file: Optional[Path] = None,
    probe_modules: tuple[str, ...] = PROBE_MODULES,
    lock_file: Optional[Path] = None,
) -> dict[str, Any]:
    """体检 → 需要时自动修复 → 复检 → import 实测。返回结构化结论，绝不抛异常。

    健康环境的开销就是一次 `check_dependencies()`（只读已安装元数据）加一次紧接着
    反正也要发生的 import，不起子进程、不碰网络。
    """
    outcome: dict[str, Any] = {
        "ok": False,
        "repair": None,
        "before": None,
        "after": None,
        "blocked_reason": None,
        "import_error": None,
        "lock": None,
    }

    before = check_dependencies(requirements)
    outcome["before"] = before
    if before.get("status") in {"ok", "unknown"}:
        import_ok, import_error = probe_imports(probe_modules)
        if import_ok:
            outcome["ok"] = True
            return outcome
        # 元数据齐全但装不起来：照样按坏环境走修复流程，理由要说清楚，否则用户只会
        # 看到"依赖都在却起不来"。
        outcome["import_error"] = import_error
        _log(f"declared dependencies are present but unusable: {import_error}")
    else:
        _log(format_report(before))

    if not allow_repair:
        outcome["blocked_reason"] = "auto-repair disabled"
        return outcome

    if not is_isolated_env():
        outcome["blocked_reason"] = (
            f"{sys.executable} is not a virtual environment; refusing to auto-install "
            "into a shared interpreter"
        )
        _log(outcome["blocked_reason"])
        return outcome

    if os.environ.get(_ATTEMPT_ENV) == "1":
        outcome["blocked_reason"] = (
            "already attempted an automatic repair in this process tree; not retrying "
            "to avoid an install loop"
        )
        _log(outcome["blocked_reason"])
        return outcome

    path = state_file if state_file is not None else state_path()
    state = _read_state(path)
    now = time.time()
    remaining = _cooldown_remaining(state, now)
    if remaining > 0:
        outcome["blocked_reason"] = (
            f"last automatic repair failed {int(now - float(state.get('last_attempt_at', now)))}s "
            f"ago; waiting {int(remaining)}s before trying again"
        )
        _log(outcome["blocked_reason"])
        return outcome

    os.environ[_ATTEMPT_ENV] = "1"

    lock = _RepairLock(
        lock_file if lock_file is not None else lock_path(),
        wait_sec=LOCK_WAIT_SEC,
    )
    status = lock.acquire()
    outcome["lock"] = {
        "status": status,
        "acquired": lock.acquired,
        "waited_sec": round(lock.waited_sec, 2),
    }
    try:
        # 只要和别人抢过锁，就说明刚有人在改同一套 site-packages —— 它很可能已经把环境
        # 修好了，再装一遍纯属浪费，还会把刚装好的文件再动一次。用 `contended` 而不是
        # `waited_sec > 0` 判断：粗粒度时钟上真的等过也可能算出 0.0。
        if lock.contended:
            _activate_site_packages()
            _reset_metadata_caches()
            recheck = check_dependencies(requirements)
            if recheck.get("status") in {"ok", "unknown"}:
                import_ok, _ = probe_imports(probe_modules)
                if import_ok:
                    outcome["after"] = recheck
                    outcome["ok"] = True
                    outcome["repair"] = {"method": "another process", "ok": True}
                    _log(
                        f"another process repaired the environment while we waited "
                        f"{lock.waited_sec:.1f}s; continuing startup"
                    )
                    return outcome

        if status == _RepairLock.TIMEOUT:
            # 别人还在装。此时自己再跑一遍 pip 就是这把锁存在的意义所在 —— 两个 pip
            # 往同一套 site-packages 写会把环境搞成半安装状态。宁可报告并让客户端重连。
            outcome["after"] = before
            outcome["blocked_reason"] = (
                f"another process has been repairing this environment for "
                f"{lock.waited_sec:.0f}s; not running a second concurrent install. "
                "Reconnect once it finishes."
            )
            _log(outcome["blocked_reason"])
            return outcome

        started_at = time.time()
        # `repair()` 自己负责"装完 import 一遍、不行就 --force-reinstall 再来一次"，
        # 而且那两轮都在这把锁里 —— 升级逻辑不能挪到锁外，否则两轮安装之间锁是放开的，
        # 别的进程正好能挤进来开第二个 pip。
        repair_result = repair(
            requirements, allow_network=allow_network, verify_modules=probe_modules
        )
        outcome["repair"] = repair_result

        _reset_metadata_caches()
        after = check_dependencies(requirements)
        import_ok, import_error = probe_imports(probe_modules)

        outcome["after"] = after
        outcome["import_error"] = import_error
        outcome["ok"] = after.get("status") in {"ok", "unknown"} and import_ok

        _write_state(path, {
            # 必须记修复"结束"的时刻，不是进入本函数的时刻。一次离线安装可能跑几十秒，
            # 用起始时刻会让冷却提前到期，客户端重启时又能立刻再装一次。
            "last_attempt_at": time.time(),
            "last_attempt_started_at": started_at,
            "last_result": "ok" if outcome["ok"] else "failed",
            "last_method": repair_result.get("method"),
            "last_error": repair_result.get("error"),
            "interpreter": sys.executable,
        })
    finally:
        lock.release()

    if outcome["ok"]:
        _log(f"environment repaired ({repair_result.get('method')}); continuing startup")
    else:
        _log(f"repair did not fix the environment: {format_report(after)}")

    return outcome


def ensure_ready(server_name: str, requirements: Optional[Path] = None) -> dict[str, Any]:
    """server 入口在 `import mcp` 之前调用的守卫。

    环境可用时直接返回，让调用方继续正常启动。修不好时**不返回** —— 直接交给
    `dependency_fallback` 起一个纯标准库的诊断 server，并在其退出后终止进程，这样
    客户端看到的是一个连得上、能说清问题、还能再试一次修复的 MCP server，而不是一条
    看不出原因的 import 异常。

    `MEMORY_MCP_NO_AUTO_REPAIR=1` 只诊断不装包；
    `MEMORY_MCP_NO_FALLBACK_SERVER=1` 关掉兜底 server（让进程按原样崩，便于排障）。
    """
    allow_repair = os.environ.get("MEMORY_MCP_NO_AUTO_REPAIR") != "1"
    allow_network = os.environ.get("MEMORY_MCP_NO_NETWORK_REPAIR") != "1"

    outcome = ensure_dependencies(
        requirements,
        allow_repair=allow_repair,
        allow_network=allow_network,
    )
    if outcome.get("ok"):
        return outcome

    if os.environ.get("MEMORY_MCP_NO_FALLBACK_SERVER") == "1":
        _log("fallback server disabled; letting startup fail as-is")
        return outcome

    _log(f"serving diagnostics for {server_name} instead of the real tool surface")
    try:
        from .dependency_fallback import serve_diagnostics

        serve_diagnostics(server_name, outcome)
    except Exception as exc:  # noqa: BLE001 — 兜底自己也不能变成不可诊断的崩溃
        _log(f"fallback server failed: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
    raise SystemExit(0)


LOCKED_PIP_BUSY_EXIT = 75  # EX_TEMPFAIL：不是安装失败，是"现在不该装"

# 部署脚本默认只肯短等。脚本自己也有超时并会在超时后杀进程树，等待时间必须留在它的预算
# 之内，否则"等锁"会先撞上脚本的超时，用户看到的是"pip 超时"而不是"别人正在装"。
DEFAULT_LOCKED_PIP_WAIT_SEC = 60.0

# 一条 `--locked-pip` 的最长时长。必须有界：这条命令是持锁跑的，而挂死的 pip 会把锁
# 一直占着，让 server 的自动修复排在后面。部署脚本自己的进程超时必须大于
# `DEFAULT_LOCKED_PIP_WAIT_SEC + 本值`，否则脚本会先把包装进程杀掉，用户看到的是
# "pip 超时"而不是"别人正在装"。
LOCKED_PIP_TIMEOUT_SEC = 600.0


def _kill_tree(proc: "subprocess.Popen[Any]") -> None:
    """连子孙一起杀。

    只 `kill()` 父进程在 Windows 上不会带走 pip 那一层，留下的 pip 会在**没有持锁**的
    情况下继续写 site-packages —— 比一开始不加锁更糟。
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=30,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass  # 落到 kill()，至少别留着不管
    try:
        proc.kill()
    except OSError:
        pass


def _locked_pip(pip_args: list[str], wait_sec: float = DEFAULT_LOCKED_PIP_WAIT_SEC) -> int:
    """持锁跑一条 pip 命令，输出直通控制台。

    部署脚本（`deploy.ps1`、`scripts/bootstrap.ps1`）原本直接调 pip，于是绕过了这把锁 ——
    而"队友刚拉到一次依赖升级、一边手动跑部署脚本、一边 IDE 里的 server 正好自动修复"
    恰恰是最容易撞上的组合。让脚本改调本入口，就能和自动路径共用同一把锁。

    不复用 `repair()`：那里会捕获 pip 输出，部署时需要的是实时进度条。这里让子进程继承
    stdio，只加一层锁和一个上界。
    """
    lock = _RepairLock(lock_path(), wait_sec=wait_sec)
    status = lock.acquire()
    try:
        if status == _RepairLock.TIMEOUT:
            _log(
                f"another process has been installing into {sys.prefix} for "
                f"{lock.waited_sec:.0f}s; not starting a second concurrent pip. "
                "Wait for it to finish, then re-run."
            )
            return LOCKED_PIP_BUSY_EXIT
        if lock.contended:
            _log(f"waited {lock.waited_sec:.1f}s for another process to finish installing")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "pip", *pip_args],
                stdin=subprocess.DEVNULL,
                env=_pip_env(),
            )
        except OSError as exc:
            _log(f"could not run pip: {exc}")
            return 1
        try:
            return proc.wait(timeout=LOCKED_PIP_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            _log(f"pip exceeded {LOCKED_PIP_TIMEOUT_SEC:.0f}s; terminating it and its children")
            _kill_tree(proc)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            return 124  # 仿 Linux timeout，与 pip 自己的退出码区分开
    finally:
        lock.release()


def _main(argv: Optional[list[str]] = None) -> int:
    """命令行入口：`python -m servers.memory_server.dependency_guard [--repair | --locked-pip ...]`。

    供部署脚本和启动探测器针对**某个具体解释器**发起体检/修复 —— 修哪个环境取决于用
    哪个 python 跑这条命令。本模块只用标准库，因此在 `mcp` 已经损坏的环境里依然可用。
    """
    import argparse

    args = list(sys.argv[1:] if argv is None else argv)
    # 手动摘出来而不交给 argparse：后面全是原样转给 pip 的参数，其中的 `-r`、`--no-index`
    # 之类会被 argparse 当成自己的选项。
    if args and args[0] == "--locked-pip":
        rest = args[1:]
        wait_sec = DEFAULT_LOCKED_PIP_WAIT_SEC
        # `--lock-wait <秒>` 必须紧跟在 `--locked-pip` 之后，这样后面无论出现什么都能
        # 原样转给 pip，不需要在 pip 的参数里找选项。
        if len(rest) >= 2 and rest[0] == "--lock-wait":
            try:
                wait_sec = float(rest[1])
            except ValueError:
                _log(f"--lock-wait needs a number of seconds, got {rest[1]!r}")
                return 2
            rest = rest[2:]
        return _locked_pip(rest, wait_sec=wait_sec)

    parser = argparse.ArgumentParser(description="Memory MCP dependency check / repair")
    parser.add_argument("--repair", action="store_true", help="repair when problems are found")
    parser.add_argument("--no-network", action="store_true", help="offline wheels only")
    parser.add_argument("--json", action="store_true", help="emit the raw report as JSON")
    parser.add_argument("--requirements", default=None, help="requirements file to check against")
    parser.add_argument("--vendor", default=None, help="offline wheel directory to install from")
    parser.add_argument(
        "--probe",
        default=",".join(PROBE_MODULES),
        help="comma-separated modules to import as the final usability check",
    )
    ns = parser.parse_args(args)

    req = Path(ns.requirements) if ns.requirements else None
    vendor = Path(ns.vendor) if ns.vendor else None
    probes = tuple(name.strip() for name in ns.probe.split(",") if name.strip())

    # 光看元数据不够：包目录被删掉而 `dist-info` 留着时体检会报"依赖正常"，而
    # `import mcp.server` 照样炸。这条命令是人和 LLM 判断"环境到底能不能用"的依据，
    # 报了 ok 就必须真的能起来。
    report = check_dependencies(req)
    healthy = report.get("status") in {"ok", "unknown"}
    import_ok, import_error = probe_imports(probes) if healthy else (False, None)
    if healthy and import_ok:
        print(json.dumps(report, indent=2) if ns.json else format_report(report))
        return 0

    print(format_report(report))
    if healthy and import_error:
        print(f"  declared dependencies are present but unusable: {import_error}")
    if not ns.repair:
        return 1

    # 显式请求：跳过冷却与"本进程树已试过"的闸门，那些是给自动路径防循环用的。跨进程锁
    # 仍然要走 —— 部署脚本和启动探测器都可能与正在启动的 server 撞上同一个 venv。
    result = locked_repair(
        requirements=req,
        allow_network=not ns.no_network,
        vendor_dir=vendor,
        # 元数据齐全却 import 不起来时，普通 `install -r` 什么都不会做。
        force_reinstall=healthy,
        # 验证与可能的第二轮安装都必须在锁内完成。
        verify_modules=probes,
    )
    _reset_metadata_caches()
    after = check_dependencies(req)
    import_ok, import_error = probe_imports(probes)
    ok = after.get("status") in {"ok", "unknown"} and import_ok

    if ns.json:
        print(json.dumps(
            {"repair": result, "after": after, "import_error": import_error}, indent=2
        ))
    else:
        for step in result.get("steps") or []:
            print(f"  {step.get('step')}: ok={step.get('ok')} - {step.get('detail')}")
        print(format_report(after))
        if not import_ok:
            print(f"  still not importable: {import_error}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
