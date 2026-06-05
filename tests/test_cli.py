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
