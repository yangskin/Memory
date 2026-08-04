from pathlib import Path


def test_bootstrap_creates_protected_local_config_without_printing_token() -> None:
    script = (Path(__file__).parents[2] / "bootstrap.sh").read_text(encoding="utf-8")

    assert 'project_id="${2:?Usage: ./bootstrap.sh <public-hostname> <project-id> [user-id]}"' in script
    assert 'docker compose -p "$project_id" up -d --build --wait' in script
    assert 'token="$(docker compose -p "$project_id" exec -T api memory-hub token create' in script
    assert 'local_config="$(dirname "$hub_dir")/user_config.local.json"' in script
    assert 'chmod 600 "$local_config"' in script
    assert 'if [ -f "$local_config" ]; then' in script
    assert 'echo "$token"' not in script