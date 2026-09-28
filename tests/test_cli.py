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


def test_missing_local_input_is_a_warning_not_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SENTIER_DATA_ROOT", str(tmp_path))
    cli.main(["validate", "bafu-ef-biosphere", "--cache-dir", str(tmp_path / "c")])
    captured = capsys.readouterr()
    assert "warning: file not found:" in captured.err
    assert f"root:   {tmp_path}" in captured.err
    assert "access: private." in captured.err
    assert "skipped bafu-ef-biosphere" in captured.err
    assert captured.out == ""


def test_run_all_skips_missing_input_sources_and_continues(tmp_path, monkeypatch, capsys):
    from sentier_importers.core import registry as registry_mod
    from sentier_importers.core.source import SourceConfig

    example = registry_mod.get_config("example-csv")
    missing = SourceConfig(
        name="needs-private-file",
        module="sentier_importers.sources.example_csv.source",
        target="sentier_inventory",
        category="example",
        fetch_url=f"file://{tmp_path / 'absent.csv'}",
        fetch_format="csv",
        output_format="json",
        access="private",
    )
    monkeypatch.setattr(cli.registry, "load_registry", lambda *a, **k: [missing, example])

    class _DefaultFetch(cli.registry.Source):
        def transform(self, records):
            return list(records)

    real_load = cli.registry.load_source
    monkeypatch.setattr(
        cli.registry,
        "load_source",
        lambda cfg: _DefaultFetch(cfg) if cfg is missing else real_load(cfg),
    )
    cli.main(
        ["run", "--all", "--cache-dir", str(tmp_path / "c"), "--output-dir", str(tmp_path / "o")]
    )
    captured = capsys.readouterr()
    assert "skipped needs-private-file" in captured.err
    assert (tmp_path / "o" / "sentier_inventory" / "example" / "example-csv.json").exists()
    assert "wrote" in captured.out
