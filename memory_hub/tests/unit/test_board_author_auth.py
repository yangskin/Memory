"""SQLite真实路由验证设备身份不可由同用户的另一凭据伪造。"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4
import importlib.util
from pathlib import Path

from sqlalchemy import select, create_engine, text, inspect
from alembic.migration import MigrationContext
from alembic.operations import Operations
from memory_hub.api.routes_board import auth_id
from memory_hub.auth.tokens import create_token
from memory_hub.db.models import AccessToken, DeviceEnrollment, BoardPost
from test_board_pagination import service, PROJECT


def enrolled(factory, device):
    tid,raw,hashed=create_token()
    with factory() as session:
        session.add(AccessToken(token_id=tid,token_secret_hash=hashed,token_prefix="test",
            user_id="alice",project_id=PROJECT,scopes=["events:write","context:read"]))
        session.add(DeviceEnrollment(enrollment_id=uuid4().hex,project_id=PROJECT,device_name=device,
            token_id=tid,token_secret_hash=hashed,poll_secret_hash="0"*64,status="approved",
            created_at=datetime.now(UTC),expires_at=datetime.now(UTC)+timedelta(days=1)))
        session.commit()
    return tid,{"Authorization":"Bearer "+raw}


def test_same_user_devices_have_server_authenticated_distinct_authors(service):
    client,factory,admin=service
    DeviceEnrollment.__table__.create(factory.kw["bind"])
    a,ha=enrolled(factory,"PC-A");b,hb=enrolled(factory,"PC-B")
    url=f"/v1/projects/{PROJECT}"
    identity=client.get(url+"/device-identities?device_name=PC-B",headers=ha).json()
    assert identity["identities"]==[{"device_name":"PC-B","author_user_id":"alice","author_auth_id":auth_id(PROJECT,b)}]
    assert client.get(url+"/device-identities",headers=ha).json()["identities"][0]["device_name"]=="PC-A"
    payload={"post_type":"note","content":"reply","source_node_name":"PC-B","author_agent_id":"codex-task-unbind"}
    forged=client.post(url+"/board/post",headers=ha,json=payload).json()["post"]
    real=client.post(url+"/board/post",headers=hb,json=payload).json()["post"]
    assert forged["author_user_id"]==real["author_user_id"]=="alice"
    assert forged["author_auth_id"]==auth_id(PROJECT,a)!=real["author_auth_id"]
    assert real["author_auth_id"]==auth_id(PROJECT,b)
    assert a not in str(forged) and b not in str(real)
    assert client.post(url+"/board/post",headers=ha,json={**payload,"author_auth_id":auth_id(PROJECT,b)}).status_code==422
    queried=client.post(url+"/board/query",headers=ha,json={"include_content":True}).json()["items"]
    assert {x["author_auth_id"] for x in queried}=={auth_id(PROJECT,a),auth_id(PROJECT,b)}
    reply=client.post(url+"/board/reply",headers=hb,json={"reply_to":real["post_id"],"content":"ok"}).json()["post"]
    assert reply["author_auth_id"]==auth_id(PROJECT,b)


def test_device_identity_rejects_revoked_cross_project_and_preserves_conflicts(service):
    client,factory,headers=service
    DeviceEnrollment.__table__.create(factory.kw["bind"])
    a,_=enrolled(factory,"PC-A");b,_=enrolled(factory,"PC-A")
    url=f"/v1/projects/{PROJECT}/device-identities"
    assert len(client.get(url+"?device_name=PC-A",headers=headers).json()["identities"])==2
    with factory() as session:
        session.get(AccessToken,b).revoked_at=datetime.now(UTC);session.commit()
    assert len(client.get(url+"?device_name=PC-A",headers=headers).json()["identities"])==1
    assert client.get(url+"?device_name=unknown",headers=headers).json()["identities"]==[]
    assert client.get("/v1/projects/other/device-identities",headers=headers).status_code==403
    assert client.get(url+"?device_name=/invalid",headers=headers).status_code==422
    assert client.get(url).status_code==401


def test_migration_preserves_legacy_board_rows_without_inventing_identity():
    path=Path(__file__).resolve().parents[2]/"migrations/versions/0014_board_author_auth.py"
    spec=importlib.util.spec_from_file_location("board_auth_migration",path)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    engine=create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE board_posts (post_id TEXT PRIMARY KEY, content TEXT)"))
        connection.execute(text("INSERT INTO board_posts VALUES ('old', 'history')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.execute(text("SELECT post_id,content,author_auth_id FROM board_posts")).one()==("old","history",None)
            migration.downgrade()
            assert connection.execute(text("SELECT * FROM board_posts")).one()==("old","history")
    engine.dispose()


def test_author_auth_id_has_a_stable_public_namespace_and_is_project_scoped():
    # 固定身份协议向量，避免重命名或重构改变已发布的身份值。
    expected = "033c8a335ae5ea34cf9fba32eb20e201cc2d93ee11e446e356e489b9f765ccbf"
    assert auth_id("project", "token-test") == expected
    assert auth_id("other-project", "token-test") != expected
    assert auth_id("project", "other-token") != expected
