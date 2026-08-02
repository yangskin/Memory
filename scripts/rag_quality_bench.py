"""One-shot RAG quality benchmark.

Spins up an isolated temp repo, seeds a richer corpus (mix of EN + 中文)
with a curated query→expected_record_id set, and runs the recall harness
against both providers (deterministic-hash baseline + local-onnx with
the downloaded bge-small-zh-v1.5 model) to surface a side-by-side
quality comparison.

Usage::

    .venv/Scripts/python.exe scripts/rag_quality_bench.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import eval_recall  # noqa: E402
from servers.memory_server.memory_config import load_config  # noqa: E402
from servers.memory_server.memory_records import memory_write_record  # noqa: E402


# Realistic 中文 + English mixed corpus reflecting actual game-dev notes.
CORPUS = {
    "material": (
        "材质管线 PBR 基础",
        "PBR 材质管线说明：基础色 BaseColor、金属度 Metallic、粗糙度 Roughness、法线 Normal "
        "map 的烘焙与压缩流程。Substance Painter 输出与 UE5 材质实例的参数对应表。",
    ),
    "audio": (
        "音频总线与子混音 Submix",
        "音频系统通过 Audio Bus 管理 Master/Music/SFX 三条主总线，子混音 Submix 用于"
        "战斗音乐自动闪避（Ducking）。MetaSound 节点图说明。",
    ),
    "input": (
        "Enhanced Input 键鼠手柄映射",
        "新输入系统 Enhanced Input：IA_Move/IA_Look/IA_Fire/IA_Jump 的 InputMappingContext "
        "在键盘鼠标和 Xbox 手柄之间的差异化映射。",
    ),
    "rendering": (
        "Lumen 全局光照与 Nanite 虚拟几何",
        "UE5 渲染管线核心：Lumen 提供动态全局光照和反射，Nanite 处理虚拟化几何体。"
        "性能调优：Lumen Scene Detail、Final Gather Quality 与 Nanite Streaming Pool。",
    ),
    "animation": (
        "动画蓝图与状态机",
        "Animation Blueprint 状态机分层：Locomotion 层（Idle/Walk/Run）+ 上半身瞄准层（"
        "Aim Offset）+ Montage 优先级。Control Rig 用于程序化 IK。",
    ),
    "networking": (
        "网络复制与 RPC",
        "Actor 复制：bReplicates、复制变量 RepNotify、RPC 三种调用（Server/Client/"
        "NetMulticast）。带宽优化：复制图 Replication Graph 与相关性 Relevancy。",
    ),
    "ai": (
        "行为树与黑板 Behavior Tree",
        "AI 行为树 Behavior Tree + 黑板 Blackboard。EQS 环境查询系统用于决策候选位置。"
        "Service 节点定期更新感知数据。",
    ),
    "build": (
        "Build.cs 模块依赖",
        "C++ 模块构建脚本 Build.cs：PublicDependencyModuleNames 与 PrivateDependency "
        "的区别，UHT 反射宏 UCLASS/USTRUCT/UENUM 的生成头依赖。",
    ),
}

# Curated queries: each one tests a different recall failure mode.
# Easy = lexical overlap with title; Medium = paraphrase; Hard = pure semantic.
CASES = [
    # Easy lexical
    {"query": "PBR 材质管线 基础色 金属度", "expected": ["material"], "tag": "easy-zh-material"},
    {"query": "Enhanced Input 键鼠手柄", "expected": ["input"], "tag": "easy-zh-input"},
    {"query": "Behavior Tree 黑板", "expected": ["ai"], "tag": "easy-zh-ai"},
    # Medium paraphrase
    {"query": "怎么做粗糙度贴图", "expected": ["material"], "tag": "med-zh-material-paraphrase"},
    {"query": "音乐自动变小 战斗触发", "expected": ["audio"], "tag": "med-zh-audio-ducking"},
    {"query": "Xbox 手柄怎么绑定开火", "expected": ["input"], "tag": "med-zh-input-pad"},
    {"query": "动态全局光照 性能调优", "expected": ["rendering"], "tag": "med-zh-lumen"},
    {"query": "怎么实现瞄准的上半身动画", "expected": ["animation"], "tag": "med-zh-aim"},
    # Hard pure-semantic (low lexical overlap)
    {"query": "玩家服务器之间怎么同步状态", "expected": ["networking"], "tag": "hard-zh-replication"},
    {"query": "AI 怎么挑一个躲避点", "expected": ["ai"], "tag": "hard-zh-eqs"},
    {"query": "新增模块需要改哪个 cs 文件", "expected": ["build"], "tag": "hard-zh-buildcs"},
    {"query": "三角形太多怎么办", "expected": ["rendering"], "tag": "hard-zh-nanite"},
]


def _seed(config) -> dict[str, str]:
    ids: dict[str, str] = {}
    for key, (title, body) in CORPUS.items():
        result = memory_write_record(
            config,
            content_markdown=f"# {title}\n\n{body}\n",
            record_kind="note",
            scope="personal",
            status="validated",
            author="bench",
            tags=["high_value"],
        )
        ids[key] = result["id"]
    return ids


def _write_cases(repo: Path, ids: dict[str, str]) -> Path:
    rows = []
    for case in CASES:
        rows.append({
            "query": case["query"],
            "expected_record_ids": [ids[k] for k in case["expected"]],
            "tag": case["tag"],
        })
    target = repo / "cases.jsonl"
    target.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8",
    )
    return target


def _bootstrap_repo(tmp_root: Path, provider: str, model_path: Path | None) -> Path:
    repo = tmp_root / f"repo_{provider.replace('-', '_')}"
    (repo / ".ai-memory").mkdir(parents=True)
    (repo / "memory-bank").mkdir()
    cfg: dict = {
        "allowed_roots": ["memory-bank"],
        "embeddings": {"enabled": True, "provider": provider},
    }
    if model_path is not None:
        cfg["embeddings"]["model_path"] = str(model_path)
    (repo / ".ai-memory" / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return repo


def _per_tag_breakdown(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        tag_class = r["tag"].split("-", 1)[0]  # easy / med / hard
        bucket = out.setdefault(tag_class, {"n": 0, "r5": 0.0, "r10": 0.0, "mrr": 0.0})
        bucket["n"] += 1
        bucket["r5"] += r["recall@5"]
        bucket["r10"] += r["recall@10"]
        bucket["mrr"] += r["rr"]
    for bucket in out.values():
        n = bucket["n"]
        bucket["r5"] /= n
        bucket["r10"] /= n
        bucket["mrr"] /= n
    return out


def run(provider: str, model_path: Path | None, tmp_root: Path) -> dict:
    repo = _bootstrap_repo(tmp_root, provider, model_path)
    config = load_config(repo)
    ids = _seed(config)
    cases_path = _write_cases(repo, ids)
    report = eval_recall.evaluate(repo, cases_path, top_k=10, rebuild_index=True)
    report["per_tag"] = _per_tag_breakdown(report["rows"])
    return report


def _fmt_report(label: str, rep: dict) -> str:
    lines = [
        f"==== {label} ====",
        f"  provider     = {rep['provider_id']}",
        f"  model_hash   = {rep['model_hash']}",
        f"  n_queries    = {rep['n_queries']}",
        f"  recall@5     = {rep['recall@5']:.3f}",
        f"  recall@10    = {rep['recall@10']:.3f}",
        f"  MRR          = {rep['mrr']:.3f}",
        "  per-difficulty:",
    ]
    for tag in ("easy", "med", "hard"):
        if tag not in rep["per_tag"]:
            continue
        b = rep["per_tag"][tag]
        lines.append(
            f"    {tag:<5} n={b['n']}  r@5={b['r5']:.3f}  r@10={b['r10']:.3f}  mrr={b['mrr']:.3f}"
        )
    lines.append("  per-query top-1:")
    for r in rep["rows"]:
        hit = "OK " if r["top"] and r["top"][0] in r["expected"] else "MISS"
        rank = next((i + 1 for i, rid in enumerate(r["top"]) if rid in r["expected"]), None)
        lines.append(f"    [{hit}] tag={r['tag']:<28} rank={rank}  q={r['query']}")
    return "\n".join(lines)


def main() -> int:
    model_path = REPO_ROOT / ".ai-memory/models/bge-small-zh-v1.5/model_quantized.onnx"
    if not model_path.exists():
        print(f"!! model not found at {model_path}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="rag_bench_") as tmp:
        tmp_root = Path(tmp)
        det = run("deterministic-hash", None, tmp_root)
        onnx = run("local-onnx", model_path, tmp_root)

    print(_fmt_report("deterministic-hash (lexical baseline)", det))
    print()
    print(_fmt_report("local-onnx bge-small-zh-v1.5", onnx))
    print()
    print("==== SUMMARY ====")
    print(f"  deterministic  recall@10={det['recall@10']:.3f}  mrr={det['mrr']:.3f}")
    print(f"  local-onnx     recall@10={onnx['recall@10']:.3f}  mrr={onnx['mrr']:.3f}")
    delta_r = onnx["recall@10"] - det["recall@10"]
    delta_m = onnx["mrr"] - det["mrr"]
    print(f"  delta          recall@10={delta_r:+.3f}  mrr={delta_m:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
