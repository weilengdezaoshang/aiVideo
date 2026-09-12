import pytest


@pytest.mark.parametrize('environment', [None, 'production', 'dev', ''])
def test_settings_closed_without_explicit_development(client, monkeypatch, environment):
    if environment is None:
        monkeypatch.delenv('SWARMUI_ENV', raising=False)
    else:
        monkeypatch.setenv('SWARMUI_ENV', environment)
    assert client.get('/api/runtime').json() == {'settingsEnabled': False}
    assert client.get('/api/config').status_code == 403
    # Rejected before parsing parameters or contacting an upstream service.
    for path in ['/api/config', '/api/config/test']:
        assert client.post(path, content='invalid json').status_code == 403
    assert client.get('/api/generation-capabilities').status_code == 200


def test_settings_available_in_development(client):
    assert client.get('/api/runtime').json() == {'settingsEnabled': True}
    assert client.get('/api/config').status_code == 200
    assert client.post('/api/config', json={'provider': 'mock'}).status_code == 200
