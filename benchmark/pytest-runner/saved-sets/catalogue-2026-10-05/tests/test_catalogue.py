def test_catalogue_returns_list(base_url, api_client):
    response = api_client.get(f"{base_url}/catalogue")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


def test_catalogue_size_returns_integer(base_url, api_client):
    response = api_client.get(f"{base_url}/catalogue/size")
    assert response.status_code == 200
    size = response.json()["size"]
    assert isinstance(size, int)


def test_catalogue_size_matches_list_length(base_url, api_client):
    list_response = api_client.get(f"{base_url}/catalogue")
    size_response = api_client.get(f"{base_url}/catalogue/size")
    assert list_response.status_code == 200
    assert size_response.status_code == 200
    items = list_response.json()
    size = size_response.json()["size"]
    assert isinstance(items, list)
    assert isinstance(size, int)
    assert len(items) == size


def test_catalogue_item_returns_object(base_url, api_client):
    response = api_client.get(f"{base_url}/catalogue")
    assert response.status_code == 200
    items = response.json()
    assert isinstance(items, list)
    assert len(items) > 0, "Cannot test /catalogue/{id} because catalogue is empty"
    first = items[0]
    if isinstance(first, dict):
        assert "id" in first, "Catalogue item is missing 'id' field"
        item_id = first["id"]
    else:
        item_id = first
    item_response = api_client.get(f"{base_url}/catalogue/{item_id}")
    assert item_response.status_code == 200
    item_data = item_response.json()
    assert isinstance(item_data, dict)
