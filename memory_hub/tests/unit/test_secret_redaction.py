from memory_hub.api import routes_board
from memory_hub.services import event_ingest
from memory_hub.services.event_ingest import _SECRET
from memory_hub.services.secret_scan import SECRET_PATTERN, contains_secret


def test_secret_detector_covers_cloud_and_token_formats() -> None:
    assert _SECRET.search("AKIAIOSFODNN7EXAMPLE")
    assert _SECRET.search("xoxb-1234567890-abcdefghij")
    assert _SECRET.search('"type": "service_account"')
    assert _SECRET.search("-----BEGIN " + "PRIVATE KEY-----")


def test_event_ingest_and_board_share_one_detector() -> None:
    # 事件写入命中时删正文、Board 命中时拒绝；两者的判定规则必须完全一致。
    assert event_ingest._SECRET is SECRET_PATTERN
    assert routes_board.contains_secret is contains_secret
    for sample in ("mongodb://user:pw@db/app", "mongodb+srv://user:pw@cluster/app",
                   "postgresql://u:p@h/d", "mysql://u:p@h/d"):
        assert contains_secret(sample), sample
    assert not contains_secret("plain status text")