def test_tags_returns_list(base_url, api_client):
    response = api_client.get(f"{base_url}/tags")
    assert response.status_code == 200
    data = response.json()["tags"]
    assert isinstance(data, list)
