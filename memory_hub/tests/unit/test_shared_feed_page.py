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
    body = response.text
    assert "/v1/shared-feed" in body
    assert "sessionStorage" in body
    assert "Bearer " in body


def test_shared_page_serves_graph_renderer() -> None:
    client = TestClient(create_app())

    page_response = client.get("/shared")
    script_response = client.get("/assets/cytoscape.min.js")

    assert '<script src="/assets/cytoscape.min.js"></script>' in page_response.text
    assert script_response.status_code == 200
    assert "cytoscape" in script_response.text[:500].lower()
    assert 'layout: { name: "cose"' in page_response.text
    assert "projectGraphDetails(nodes, edges)" in page_response.text
    assert 'node.type === "task" || degree[node.id] !== 1' in page_response.text
    assert "members.length < 2 || expandedGraphGroups[key]" in page_response.text
    assert 'expandedGraphGroups[selected.data("key")] = true' in page_response.text
    assert 'selected.removeClass("faded").addClass("focused")' in page_response.text
    assert 'selected.connectedEdges().removeClass("faded").addClass("context")' in page_response.text
    assert "selected.closedNeighborhood()" not in page_response.text
