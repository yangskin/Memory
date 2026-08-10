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
    assert 'FEED_CACHE_KEY = "memory_hub_shared_feed_cache_v2"' in body
    assert "loadFeedCache(feed.project_id)" in body
    assert "sessionStorage.removeItem(FEED_CACHE_KEY)" in body
    assert "events_from_history" in body


def test_shared_page_serves_dark_paginated_task_queue_and_selected_lineage_without_legacy_graph() -> None:
    client = TestClient(create_app())

    page_response = client.get("/shared")
    script_response = client.get("/assets/cytoscape.min.js")

    assert '<script src="/assets/cytoscape.min.js"></script>' in page_response.text
    assert script_response.status_code == 200
    assert "cytoscape" in script_response.text[:500].lower()
    assert 'layout: { name: "breadthfirst"' in page_response.text
    assert 'data-tab="graphPanel"' not in page_response.text
    assert "function fetchGraph()" not in page_response.text
    assert "/graph?include_metadata" not in page_response.text
    assert 'data-tab="taskPanel"' in page_response.text
    assert 'id="taskBoard"' in page_response.text
    assert 'id="taskLineageArea"' in page_response.text
    assert 'id="taskOpenCount"' in page_response.text
    assert 'id="taskBlockedCount"' in page_response.text
    assert 'class="task-table-wrap"' in page_response.text
    assert 'id="taskLoadMoreBtn"' in page_response.text
    assert 'id="taskAgents"' in page_response.text
    assert 'id="taskEvents"' in page_response.text
    assert "function fetchTaskWorkspace(append)" in page_response.text
    assert "function renderTaskWorkspace(catalog)" in page_response.text
    assert "function renderTaskLineage(bundle, record)" in page_response.text
    assert "function selectTask(taskId)" in page_response.text
    assert 'data-task-id="' in page_response.text
    assert '"/tasks?" + query' in page_response.text
    assert '"/task-graph?task_id="' in page_response.text
    assert '"/task-events?task_id="' in page_response.text
    assert "TASK_SHAPES" in page_response.text
    assert "color-scheme: dark" in page_response.text
    assert 'id="themeBtn"' not in page_response.text
    assert "data-theme" not in page_response.text
