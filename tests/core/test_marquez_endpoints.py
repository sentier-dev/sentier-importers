import httpx
import pytest
from sentier_importers import __main__ as cli
from sentier_importers.core import inventory_lineage
from sentier_importers.core.errors import SentierImporterError

from tests.core.test_inventory_lineage import run_inventory


def mock_server(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(
        inventory_lineage.httpx,
        "Client",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.parametrize(
    "common,base",
    [
        ("https://lineage.example.org", "https://lineage.example.org"),
        ("https://lineage.example.org/prefix/api/v1/", "https://lineage.example.org/prefix"),
    ],
)
def test_common_endpoint_and_read_only_query(tmp_path, monkeypatch, capsys, common, base):
    _, artifact, *_ = run_inventory(tmp_path, local=False)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"  # No writes to a public server.
        return httpx.Response(200, json={"graph": [{"id": "output"}]})

    mock_server(monkeypatch, handle)
    cli.main(
        ["inspect-lineage", "--artifact", str(artifact), "--marquez-url", common, "--no-upload"]
    )
    assert len(requests) == 1
    assert str(requests[0].url).startswith(base + "/api/v1/lineage?")
    assert f"Marquez: {base}/lineage/dataset/" in capsys.readouterr().out


def test_split_overrides_take_precedence(tmp_path, monkeypatch, capsys):
    _, artifact, *_ = run_inventory(tmp_path, local=False)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"graph": [{}]})

    mock_server(monkeypatch, handle)
    cli.main(
        [
            "inspect-lineage",
            "--artifact",
            str(artifact),
            "--marquez-url",
            "https://common.example.org",
            "--marquez-api",
            "https://api.example.org",
            "--marquez-web",
            "https://web.example.org",
        ]
    )
    assert all(request.url.host == "api.example.org" for request in requests)
    assert any(request.method == "POST" for request in requests)
    assert "Marquez: https://web.example.org/lineage/dataset/" in capsys.readouterr().out


@pytest.mark.parametrize("status,body", [(404, {}), (200, {"graph": []})])
def test_read_only_missing_graph_is_explicit(tmp_path, monkeypatch, status, body):
    _, artifact, *_ = run_inventory(tmp_path, local=False)
    mock_server(monkeypatch, lambda request: httpx.Response(status, json=body))
    with pytest.raises(SentierImporterError, match="not indexed"):
        inventory_lineage.inspect_artifact(
            artifact, "https://example.org", "https://example.org", upload=False
        )


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/marquez",
        "https://user:secret@example.org",
        "https://example.org/?token=secret",
    ],
)
def test_invalid_endpoints_do_not_contact_server(tmp_path, monkeypatch, url):
    _, artifact, *_ = run_inventory(tmp_path, local=False)
    mock_server(monkeypatch, lambda request: pytest.fail("Unexpected request"))
    with pytest.raises(SentierImporterError, match="base URLs"):
        inventory_lineage.inspect_artifact(artifact, url, url)
