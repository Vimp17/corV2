"""Standalone identity and settings contracts; no real foreign process is touched."""
import json
from pathlib import Path
from types import SimpleNamespace
from standalone import envfile
from standalone.cli import service


def test_environment_round_trip_preserves_comments_unicode_and_secrets(tmp_path):
    template = tmp_path/'template'
    template.write_text('# Настройки\nNAME=old\n# OPTIONAL=\n')
    target = tmp_path/'.env'
    values = {'NAME':'тест', 'OPTIONAL':'a=b#c', 'EXTRA':'value'}
    target.write_text(envfile.render(template,values))
    assert envfile.read(target) == values
    assert '# Настройки' in target.read_text()
    target.write_bytes(envfile.BOM + target.read_bytes())
    assert envfile.normalize(target)
    assert envfile.read(target) == values


def test_pid_creation_identity_must_match_saved_instance(tmp_path, monkeypatch):
    path = tmp_path/'api.pid'
    monkeypatch.setattr(service,'PID_FILE',path)
    monkeypatch.setattr(service,'_identity',lambda pid:'creation-1')
    service._save_pid(123)
    assert service._read_pid() == 123 and service._alive(123)
    monkeypatch.setattr(service,'_identity',lambda pid:'creation-2')
    assert not service._alive(123)


def test_failed_stop_keeps_identity_and_blocks_restart(tmp_path, monkeypatch):
    path = tmp_path/'api.pid'; path.write_text('{}')
    monkeypatch.setattr(service,'PID_FILE',path)
    monkeypatch.setattr(service,'env_values',lambda:{})
    monkeypatch.setattr(service,'_read_pid',lambda:123)
    monkeypatch.setattr(service,'_alive',lambda pid:True)
    monkeypatch.setattr(service,'_terminate',lambda pid:None)
    monkeypatch.setattr(service,'cmd_start',lambda args:(_ for _ in ()).throw(AssertionError('must not restart')))
    assert service.cmd_restart(SimpleNamespace()) == 1
    assert path.exists()
