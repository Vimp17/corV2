"""Source setup/check contracts, specified before implementing the CLI."""
from argparse import Namespace

import pytest

from standalone import envfile
from standalone.cli.common import Stop


def arguments(**kwargs):
    return Namespace(action="configure", source="dars", url=None, token=None, well_ids=None,
                     non_interactive=True, well=None, **kwargs)


def test_configuration_preserves_other_settings_and_backs_up(tmp_path, monkeypatch, capsys):
    from standalone.cli import sources
    path = tmp_path / ".env"
    original = "MAI_DATABASE_URL=untouched\nMAI_API_KEY=keep-key\nMAI_SOURCE_DARS_TOKEN=old-secret\nCUSTOM_SETTING=keep\n"
    path.write_text(original)
    monkeypatch.setattr(sources, "ENV_FILE", path)
    args = arguments()
    args.url, args.token, args.well_ids = "https://example.test/dars", "new-secret", "A, B, A"
    assert sources.cmd_sources(args) == 0
    values = envfile.read(path)
    assert values["MAI_DATABASE_URL"] == "untouched" and values["CUSTOM_SETTING"] == "keep"
    assert values["MAI_API_KEY"] == "keep-key" and values["MAI_SOURCE_DARS_TOKEN"] == "new-secret"
    assert values["MAI_WELL_IDS"] == "A,B"
    assert path.with_name(".env.bak").read_text() == original
    assert "new-secret" not in capsys.readouterr().out
    args.url, args.token, args.well_ids = None, "-", None
    sources.cmd_sources(args)
    assert envfile.read(path).get("MAI_SOURCE_DARS_TOKEN", "") == ""
    assert envfile.read(path)["MAI_SOURCE_DARS_URL"] == "https://example.test/dars"


@pytest.mark.parametrize("url", ["https://[broken", "ftp://example/data", "http://x:bad/data", "https://x/\nMAI_API_KEY=bad"])
def test_invalid_endpoint_does_not_modify_env(tmp_path, monkeypatch, url):
    from standalone.cli import sources
    path = tmp_path / ".env"
    path.write_text("MAI_API_KEY=keep\n")
    monkeypatch.setattr(sources, "ENV_FILE", path)
    args = arguments()
    args.url = url
    with pytest.raises(Stop):
        sources.cmd_sources(args)
    assert path.read_text() == "MAI_API_KEY=keep\n" and not path.with_name(".env.bak").exists()


def test_check_uses_saved_well_and_never_imports(tmp_path, monkeypatch, capsys):
    from standalone.cli import sources
    from app.core import source_connectors
    path = tmp_path / ".env"
    path.write_text("MAI_SOURCE_DARS_URL=https://example.test/data\nMAI_WELL_IDS=FIRST,SECOND\n")
    monkeypatch.setattr(sources, "ENV_FILE", path)
    calls = []
    def check(profile, well):
        calls.append((profile, well))
        return {"status": "error", "message": "HTTP 401. Проверьте токен.", "record_count": 0}
    monkeypatch.setattr(source_connectors, "check_source", check)
    args = arguments()
    args.action = "check"
    assert sources.cmd_sources(args) == 1
    assert calls == [("dars", "FIRST")]
    assert "401" in capsys.readouterr().out


def test_cli_parser_exposes_source_commands():
    from standalone.manage import parser
    args = parser().parse_args(["sources", "check", "--source", "dars", "--well", "A"])
    assert args.action == "check" and args.well == "A"
