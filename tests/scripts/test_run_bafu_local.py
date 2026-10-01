"""Tests for ``scripts/run_bafu_local.py`` metadata emission."""

import importlib.util
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_bafu_local.py"


@pytest.fixture(scope="module")
def run_bafu_local():
    spec = importlib.util.spec_from_file_location("run_bafu_local", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage(folder: Path, sources: list[str]) -> None:
    folder.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "process_id": [f"p{i}" for i in range(len(sources))],
                "source": sources,
                "source_version": ["v1"] * len(sources),
            }
        ),
        folder / "processes.parquet",
    )
    pq.write_table(
        pa.table({"process_id": ["p0", "p0"], "amount": [1.0, 2.0]}),
        folder / "exchanges.parquet",
    )


def test_sector_metadata_lists_sources_from_staged_parquet(tmp_path, run_bafu_local):
    folder = tmp_path / "02-electricity"
    _stage(folder, ["bafu-2026", "bafu-2026"])
    meta = run_bafu_local.sector_metadata("02-electricity", folder)
    assert meta["sources"] == ["bafu-2026"]
    assert meta["schema_version"] == "0.2.0"
    assert meta["sector"] == "electricity"
    assert meta["rank"] == 2
    assert meta["row_counts"] == {"processes.parquet": 2, "exchanges.parquet": 2}


def test_sector_metadata_sources_are_distinct_and_sorted(tmp_path, run_bafu_local):
    # a folder staged by two importers lists both sources, once each, in a stable order
    folder = tmp_path / "01-agriculture"
    _stage(folder, ["other-1", "bafu-2026", "other-1"])
    meta = run_bafu_local.sector_metadata("01-agriculture", folder)
    assert meta["sources"] == ["bafu-2026", "other-1"]


def test_sector_metadata_without_processes_file_has_no_sources(tmp_path, run_bafu_local):
    folder = tmp_path / "03-chemicals"
    folder.mkdir()
    meta = run_bafu_local.sector_metadata("03-chemicals", folder)
    assert meta["sources"] == []
    assert meta["row_counts"] == {}
