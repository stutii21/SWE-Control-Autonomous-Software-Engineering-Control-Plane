from deploy import is_production, target_environment


def test_default_is_staging(monkeypatch):
    monkeypatch.delenv("DEPLOY_ENV", raising=False)
    assert target_environment() == "staging"
    assert is_production() is False
