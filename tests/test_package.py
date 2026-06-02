import sentier_importers


def test_version_is_exposed():
    assert isinstance(sentier_importers.__version__, str)
    assert sentier_importers.__version__.count(".") >= 1
