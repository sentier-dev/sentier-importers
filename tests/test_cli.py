import pytest
import sentier_importers.__main__ as cli


def test_list_prints_sources(capsys):
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "example-csv" in out
    assert "sentier_inventory" in out


def test_validate_reports_row_count(tmp_path, capsys):
    cli.main(["validate", "example-csv", "--cache-dir", str(tmp_path / "c")])
    out = capsys.readouterr().out
    assert "3" in out


def test_run_dry_run_emits_file(tmp_path, capsys):
    cli.main(
        [
            "run",
            "example-csv",
            "--cache-dir",
            str(tmp_path / "c"),
            "--output-dir",
            str(tmp_path / "o"),
        ]
    )
    assert (tmp_path / "o" / "sentier_inventory" / "example" / "example-csv.json").exists()


def test_run_all_dry_run(tmp_path):
    cli.main(
        ["run", "--all", "--output-dir", str(tmp_path / "o"), "--cache-dir", str(tmp_path / "c")]
    )
    assert (tmp_path / "o" / "sentier_inventory" / "example" / "example-csv.json").exists()


def test_unknown_source_exits_nonzero(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "nope", "--output-dir", str(tmp_path / "o")])
    assert exc.value.code != 0


def test_list_prints_access_column(capsys):
    cli.main(["list"])
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("bafu-ef-biosphere "))
    assert "private" in line
    assert "public" in next(ln for ln in out.splitlines() if ln.startswith("example-csv "))


def test_data_root_flag_reaches_the_registry(tmp_path, monkeypatch):
    seen = {}
    real = cli.registry.load_registry

    def spy(path=cli.registry.REGISTRY_PATH, data_root=None):
        seen["root"] = data_root
        return real(path, data_root=data_root)

    monkeypatch.setattr(cli.registry, "load_registry", spy)
    for argv in (
        ["list", "--data-root", str(tmp_path)],
        [
            "validate",
            "example-csv",
            "--data-root",
            str(tmp_path),
            "--cache-dir",
            str(tmp_path / "c"),
        ],
        [
            "run",
            "--all",
            "--data-root",
            str(tmp_path),
            "--cache-dir",
            str(tmp_path / "c"),
            "--output-dir",
            str(tmp_path / "o"),
        ],
    ):
        seen.clear()
        cli.main(argv)
        assert seen["root"] == tmp_path.resolve(), argv


def test_data_root_env_var_is_reported_in_the_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SENTIER_DATA_ROOT", str(tmp_path))
    with pytest.raises(SystemExit):
        cli.main(["validate", "bafu-ef-biosphere", "--cache-dir", str(tmp_path / "c")])
    err = capsys.readouterr().err
    assert f"root:   {tmp_path}" in err
    assert "access: private." in err
