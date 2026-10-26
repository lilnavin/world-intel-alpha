import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from worldintel import delivery


@pytest.mark.parametrize("adapter,prefix", [("hermes-cron", "MEDIA:"), ("stdout", ""), ("file", None)])
def test_local_delivery(adapter, prefix, tmp_path, capsys):
    path = tmp_path / "brief.html"
    path.write_text("<html>fixture</html>", encoding="utf-8")
    delivery.deliver(adapter, str(path), lambda _: None)
    expected = "" if prefix is None else f"{prefix}{path}\n"
    assert capsys.readouterr().out == expected


def test_missing_file_never_emits_media(tmp_path, capsys):
    with pytest.raises(delivery.DeliveryError):
        delivery.deliver("hermes-cron", str(tmp_path / "absent.html"), lambda _: None)
    assert capsys.readouterr().out == ""


def test_unknown_policy_never_silently_falls_back(tmp_path, capsys):
    with pytest.raises(delivery.DeliveryError, match="unsupported"):
        delivery.deliver("hermes_cron", str(tmp_path / "brief.html"), lambda _: None)
    assert capsys.readouterr().out == ""
