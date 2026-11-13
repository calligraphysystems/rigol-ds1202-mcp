"""Critical metadata must be bound to the saved payload before any use."""
import json
from pathlib import Path
import pytest

from rigol_mcp.advanced import Advanced
from rigol_mcp.artifacts import Artifacts
from rigol_mcp.integrity import seal
from rigol_mcp.scope import Scope
from rigol_mcp.simulator import Simulator


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    sim = Simulator()
    sim.memory_points = 100
    return sim, Advanced(Scope(sim.connect, allow_control=True, simulated=True), Artifacts(tmp_path/'screenshots'))


def edit(path, field, value):
    record = json.loads(path.read_text())
    current = record
    for key in field[:-1]:
        current = current[key]
    current[field[-1]] = value
    path.write_text(json.dumps(record))


@pytest.mark.parametrize('field,value', [
    (['verified_settings', ':TIM:MAIN:SCAL'], '0.002'),
    (['instrument', 'serial'], 'OTHER'), (['simulated'], False),
    (['acquisition_at_save'], 'RUN'), (['artifact_schema_version'], 2),
    (['artifact_schema_version'], True), (['artifact_id'], 'a'*32)])
def test_altered_setup_refused_before_any_instrument_access(rig, field, value):
    sim, advanced = rig
    saved = advanced.read('setup.snapshot', {})
    path = advanced.setups.root/(saved['artifact_id']+'.json')
    edit(path, field, value)
    sim.commands.clear()
    with pytest.raises(ValueError, match='integrity'):
        advanced.control('setup.restore', {'artifact_id': saved['artifact_id']})
    assert not sim.commands


@pytest.mark.parametrize('field,value', [
    (['scaling', 'y_increment'], .4), (['scaling', 'x_increment'], .01),
    (['amplitude_unit'], 'A'), (['instrument', 'serial'], 'OTHER'),
    (['simulated'], False), (['summary', 'maximum'], 99)])
@pytest.mark.parametrize('operation', ['waveform.info', 'waveform.samples', 'analysis.fft', 'waveform.overview', 'waveform.image'])
def test_altered_waveform_refused_for_every_numeric_reader(rig, field, value, operation):
    sim, advanced = rig
    saved = advanced.control('waveform.full_memory', {'channel': 1})
    path = advanced.waveforms.root/(saved['artifact_id']+'.json')
    edit(path, field, value)
    sim.commands.clear()
    with pytest.raises(ValueError, match='integrity'):
        advanced.read(operation, {'artifact_id': saved['artifact_id']})
    assert not sim.commands


@pytest.mark.parametrize('field,value', [(['quality', 'status'], 'decoded'), (['restoration', 'ok'], True)])
def test_decode_result_quality_and_restoration_are_protected(rig, field, value):
    sim, advanced = rig
    saved = advanced.bus_results.put(b'{"events": []}', {'ok': False, 'artifact_type': 'serial.decode',
        'quality': {'status': 'suspect'}, 'restoration': {'ok': False}})
    path = advanced.bus_results.root/(saved['artifact_id']+'.json')
    edit(path, field, value)
    with pytest.raises(ValueError, match='integrity'):
        advanced.read('decode.result', {'artifact_id': saved['artifact_id']})
    assert not sim.commands


def test_screenshot_identity_is_protected(rig):
    sim, advanced = rig
    saved = advanced.read('screenshot.capture', {})
    path = advanced.artifacts.root/(saved['artifact_id']+'.json')
    edit(path, ['instrument', 'serial'], 'OTHER')
    sim.commands.clear()
    with pytest.raises(ValueError, match='integrity'):
        advanced.read('artifact.image', {'artifact_id': saved['artifact_id']})
    assert not sim.commands


@pytest.mark.parametrize('kind', ['setup', 'waveform', 'screenshot', 'result', 'decoder'])
def test_legacy_artifacts_are_refused_without_migration_or_instrument_access(rig, kind):
    sim, advanced = rig
    if kind == 'setup':
        saved = advanced.read('setup.snapshot', {})
        store, reader, operation = advanced.setups, advanced.control, 'setup.restore'
    elif kind == 'waveform':
        saved = advanced.control('waveform.full_memory', {'channel': 1})
        store, reader, operation = advanced.waveforms, advanced.read, 'waveform.samples'
    elif kind == 'screenshot':
        saved = advanced.read('screenshot.capture', {})
        store, reader, operation = advanced.artifacts, advanced.read, 'artifact.image'
    elif kind == 'result':
        saved = advanced.bus_results.put(b'{"events": []}', {'artifact_type': 'serial.decode'})
        store, reader, operation = advanced.bus_results, advanced.read, 'decode.result'
    else:
        configured = advanced.control('decode.uart.configure', {'decoder': 1, 'tx': 'CHAN1', 'rx': 'OFF', 'baud': 9600})
        saved = {'artifact_id': configured['rollback_artifact_id']}
        store, reader, operation = advanced.decoders, advanced.control, 'decode.restore'
    path = store.root/(saved['artifact_id']+'.json')
    record = json.loads(path.read_text())
    del record['artifact_schema_version'], record['metadata_sha256']
    path.write_text(json.dumps(record))
    before = path.read_bytes()
    sim.commands.clear()
    with pytest.raises(ValueError, match='Legacy'):
        reader(operation, {'artifact_id': saved['artifact_id']})
    assert not sim.commands and path.read_bytes() == before


def test_waveform_cross_field_consistency_even_with_valid_metadata_checksum(rig):
    _, advanced = rig
    saved = advanced.control('waveform.full_memory', {'channel': 1})
    path = advanced.waveforms.root/(saved['artifact_id']+'.json')
    record = json.loads(path.read_text())
    record['scaling']['y_increment'] *= 10
    path.write_text(json.dumps(seal(record)))
    with pytest.raises(ValueError, match='preamble'):
        advanced.read('waveform.samples', {'artifact_id': saved['artifact_id']})
