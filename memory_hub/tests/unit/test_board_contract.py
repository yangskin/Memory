from uuid import UUID

from memory_hub.domain.board import BoardPostRequest, BoardQueryRequest, BoardReplyRequest


def test_blank_optional_board_values_are_treated_as_omitted() -> None:
    post = BoardPostRequest(
        post_type="note",
        content="hello",
        post_id="",
        task_id="",
        thread_id="",
        expires_at="",
        author_agent_id="",
        author_agent_instance_id="",
    )
    assert post.task_id is None
    assert post.post_id is None
    assert post.thread_id is None
    assert post.expires_at is None
    assert post.author_agent_id is None
    assert post.author_agent_instance_id is None

    reply = BoardReplyRequest(content="reply", thread_id="", reply_to="")
    assert reply.thread_id is None
    assert reply.reply_to is None

    query = BoardQueryRequest(
        user_id="",
        agent_instance_id="",
        task_id="",
        status="",
        post_type="",
        thread_id="",
    )
    assert query.user_id is None
    assert query.agent_instance_id is None
    assert query.task_id is None
    assert query.status is None
    assert query.post_type is None
    assert query.thread_id is None


def test_client_generated_post_id_is_accepted() -> None:
    value = "54098ef0-0bfe-4a2a-9927-d606ca0be649"
    post = BoardPostRequest(post_id=value, post_type="question", content="sync me")
    assert post.post_id == UUID(value)
