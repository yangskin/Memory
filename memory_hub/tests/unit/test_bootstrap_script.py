from pathlib import Path


def test_bootstrap_creates_protected_local_config_without_printing_token() -> None:
    script = (Path(__file__).parents[2] / "bootstrap.sh").read_text(encoding="utf-8")

    assert 'project_id="${2:?Usage: ./bootstrap.sh <public-hostname> <project-id> [user-id]}"' in script
    assert 'docker compose -p "$project_id" up -d --build --wait' in script
    assert 'token="$(docker compose -p "$project_id" exec -T api memory-hub token create' in script
    assert 'local_config="$(dirname "$hub_dir")/user_config.local.json"' in script
    assert 'shared_memory_config="$(dirname "$hub_dir")/shared_memory.local.json"' in script
    assert 'chmod 600 "$local_config" "$shared_memory_config"' in script
    assert 'if [ -f "$local_config" ] || [ -f "$shared_memory_config" ]; then' in script
    assert 'label=com.docker.compose.project.working_dir=$hub_dir' in script
    assert 'grep -Fvx "$project_id"' in script
    assert "Another Memory Hub Compose project is already running" in script
    assert 'echo "$token"' not in script


def test_memory_test_python_resolver_supports_linux_virtualenv() -> None:
    script = (Path(__file__).parents[3] / "scripts" / "Resolve-MemoryTestPython.ps1").read_text(
        encoding="utf-8"
    )

    assert 'Join-Path $MemoryRoot ".venv/bin/python"' in script
    assert 'Join-Path $RepoRoot ".venv/bin/python"' in script