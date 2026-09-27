import home_disconnect


def test_version_is_set() -> None:
    assert isinstance(home_disconnect.__version__, str)
    assert home_disconnect.__version__


def test_public_api() -> None:
    for name in home_disconnect.__all__:
        assert hasattr(home_disconnect, name), name
