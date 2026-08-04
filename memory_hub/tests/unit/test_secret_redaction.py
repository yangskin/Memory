from memory_hub.services.event_ingest import _SECRET


def test_secret_detector_covers_cloud_and_token_formats() -> None:
    assert _SECRET.search("AKIAIOSFODNN7EXAMPLE")
    assert _SECRET.search("xoxb-1234567890-abcdefghij")
    assert _SECRET.search('"type": "service_account"')
    assert _SECRET.search("-----BEGIN " + "PRIVATE KEY-----")