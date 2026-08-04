from memory_hub.auth.tokens import create_token, parse_token, verify_secret


def test_created_token_round_trips_without_storing_raw_secret() -> None:
    token_id, token, stored_hash = create_token()
    parsed = parse_token(token)

    assert parsed is not None
    assert parsed.token_id == token_id
    assert parsed.secret not in stored_hash
    assert verify_secret(parsed.secret, stored_hash)


def test_invalid_token_format_and_secret_are_rejected() -> None:
    assert parse_token("not-a-memory-token") is None
    _, token, stored_hash = create_token()
    parsed = parse_token(token)
    assert parsed is not None
    assert not verify_secret(f"wrong-{parsed.secret}", stored_hash)