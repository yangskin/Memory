"""Static shared-dashboard page is served without any database."""

from __future__ import annotations

from fastapi.testclient import TestClient

from memory_hub.api.main import create_app


def test_shared_page_is_served_without_database() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get("/shared")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store, max-age=0"
    body = response.text
    assert "/v1/shared-feed" in body
    assert "sessionStorage" in body
    assert "Bearer " in body
    assert "memory_hub_shared_feed_cache" in body
    assert "loadFeedCache(feed.project_id)" in body
    assert "sessionStorage.removeItem(FEED_CACHE_KEY)" in body
    assert "events_from_history" in body


def test_shared_page_serves_graph_renderer() -> None:
    client = TestClient(create_app())

    page_response = client.get("/shared")
    script_response = client.get("/assets/cytoscape.min.js")

    assert '<script src="/assets/cytoscape.min.js"></script>' in page_response.text
    assert script_response.status_code == 200
    assert "cytoscape" in script_response.text[:500].lower()
    assert 'layout: { name: "cose"' in page_response.text
    assert "projectGraphDetails(nodes, edges)" in page_response.text
    assert 'if (degree[node.id] !== 1) return;' in page_response.text
    assert 'node.type === "task"' not in page_response.text
    assert "members.length < 2 || expandedGraphGroups[key]" in page_response.text
    assert 'expandedGraphGroups[selected.data("key")] = true' in page_response.text
    assert 'selected.removeClass("faded").addClass("focused")' in page_response.text
    assert 'selected.connectedEdges().removeClass("faded").addClass("context")' in page_response.text
    assert "selected.closedNeighborhood()" not in page_response.text
    assert "共同记忆补充" not in page_response.text
    assert "graph-mode-btn" not in page_response.text
    assert "共同记忆来源" in page_response.text
    assert "edge[relation = 'documents']" in page_response.text
    assert "暂无带实体标注的共同记忆。" in page_response.text
