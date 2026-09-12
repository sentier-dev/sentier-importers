from sentier_importers.core.randonneur import codes_of


def test_codes_of_collects_replace_and_update_entries_that_carry_a_code():
    package = {
        "name": "x",
        "version": "0",
        "replace": [{"source": {"code": "a"}, "target": {"code": "t1"}}],
        "update": [{"source": {"code": "b"}, "target": {"code": "t2"}}],
    }
    assert codes_of(package) == {"a", "b"}


def test_codes_of_skips_entries_missing_a_source_code():
    package = {
        "name": "x",
        "version": "0",
        "replace": [
            {"source": {"code": "a"}, "target": {"code": "t1"}},
            {"source": {}, "target": {"code": "t2"}},
            {"target": {"code": "t3"}},
        ],
    }
    assert codes_of(package) == {"a"}


def test_codes_of_empty_package_yields_no_codes():
    assert codes_of({"name": "x", "version": "0"}) == set()
