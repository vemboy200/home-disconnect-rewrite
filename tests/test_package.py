import home_disconnect


def test_version_is_set() -> None:
    assert isinstance(home_disconnect.__version__, str)
    assert home_disconnect.__version__
