from contextlib import contextmanager
from pathlib import Path
import json
import numpy as np
import pytest

from rigol_mcp.serial_decode import decode
from rigol_mcp.bus_workflow import plan
from rigol_mcp.advanced import Advanced
from rigol_mcp.artifacts import Artifacts
from rigol_mcp.scope import Scope
from rigol_mcp.simulator import Simulator
from rigol_mcp.workflows import settings_fingerprint, differences


def uart(values, samples=100, bad_stop=False):
    bits=[1]*40
    for value in values:
        bits += [0]+[(value >> k)&1 for k in range(8)]+[0 if bad_stop else 1]
    bits += [1]*40
    return np.repeat(bits,samples).astype(bool)


def spi(values, samples=100):
    clk, data = [0]*2000, [0]*2000
    for value in values:
        for bit in range(7,-1,-1):
            clk += [0]*(samples//2)+[1]*(samples//2)
            data += [(value >> bit)&1]*samples
        clk += [0]*2000
        data += [value&1]*2000
    return np.array(clk,bool), np.array(data,bool)


def i2c():
    clk,data=[1]*40,[1]*40
    clk += [1]*20;data += [0]*20  # START
    for value in (0xA0,0x12,0xA5,0x5A):
        for bit in [((value >> n)&1) for n in range(7,-1,-1)]+[0]:
            clk += [0]*20+[1]*20+[0]*20
            data += [bit]*60
    clk += [0]*20+[1]*40;data += [0]*40+[1]*20  # STOP
    return np.array(clk,bool),np.array(data,bool)


def test_uart_all_bytes_and_stop_bits():
    expected=bytes(range(256))
    r=decode('uart',uart(expected),None,1e-6,0,10000,.0016)
    assert bytes(e['data'] for e in r['events'])==expected
    assert all(e['stop_valid'] for e in r['events']) and not r['flags']


def test_uart_stop_error_and_truncated_frame():
    r=decode('uart',uart([0],bad_stop=True),None,1e-6,0,10000,.0016)
    assert 'uart_stop_error' in r['flags']
    r=decode('uart',uart([0x55])[:4500],None,1e-6,0,10000,.0016)
    assert not r['events'] and r['boundary_fragments']


def test_uart_does_not_guess_framing_from_middle_of_packet():
    r=decode('uart',uart([0x55]*10)[4500:-4000],None,1e-6,0,10000,.0016)
    assert not r['events'] and 'uart_idle_boundary_missing_or_incomplete_frame' in r['flags']


def test_spi_full_pattern_and_setup_violation():
    c,d=spi([0xA5,0x5A,0,255])
    r=decode('spi',d,c,1e-6,0,10000,.0016)
    assert [e['data'] for e in r['events']]==[0xA5,0x5A,0,255] and not r['flags']
    d[2050]=~d[2050]
    assert 'spi_data_changes_at_sampling_edge' in decode('spi',d,c,1e-6,0,10000,.0016)['flags']


def test_spi_drops_unbounded_edge_fragments():
    c,d=spi([0xA5,0x5A])
    r=decode('spi',d[1900:-1900],c[1900:-1900],1e-6,0,10000,.0016)
    assert not r['events'] and r['boundary_fragments']==2


def test_i2c_address_data_ack_stop():
    c,d=i2c()
    r=decode('i2c',d,c,1e-5,0,2000,.008)
    assert not r['flags']
    e=r['events'][0]
    assert e['address']==0x50 and e['direction']=='write'
    assert e['data']==[0x12,0xA5,0x5A] and e['acknowledged']==[True]*4 and e['end']=='stop'


def test_i2c_read_and_nack_preserved():
    c,d=i2c()
    # R/W bit of the observed address byte, including the low setup interval.
    d[60+7*60:60+8*60]=True
    # Final data ACK slot is a NACK, legitimate at the end of a read.
    d[60+35*60:60+36*60]=True
    e=decode('i2c',d,c,1e-5,0,2000,.008)['events'][0]
    assert e['direction']=='read' and e['address']==0x50
    assert e['acknowledged']==[True,True,True,False]


def test_insufficient_sampling_never_decodes():
    r=decode('uart',uart([0x55]),None,.001,0,9600,.01)
    assert not r['events'] and r['flags']==['insufficient_sample_rate']


@pytest.mark.parametrize('extra',[
    {'clock_channel':2}, {'expected_hex':'zz'}, {'expected_hex':''},
    {'window_seconds':.5,'bit_rate':1000000}, {'frame_gap_seconds':.00001}])
def test_dependent_validation(extra):
    with pytest.raises(ValueError):plan({'protocol':'spi','bit_rate':10000,**extra})


@pytest.fixture
def rig(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    sim=Simulator();sim.memory_points=60000;sim.settings[':ACQ:MDEP']='60000'
    sim.single_polls_remaining=0
    packet=uart(b'\x55\xA5\x00\xffRigol\n')
    raw=np.where(np.pad(packet,(0,60000-len(packet)),constant_values=True),210,127).astype('uint8').tobytes()
    binary=sim.binary
    def replacement(command,maximum):
        if command==':WAV:DATA?' and sim.settings[':WAV:MODE']=='RAW':
            start,end=int(sim.settings[':WAV:STAR'])-1,int(sim.settings[':WAV:STOP'])
            return raw[start:end]
        return binary(command,maximum)
    sim.binary=replacement
    connections=[]
    @contextmanager
    def connect():
        connections.append(1)
        yield sim
    scope=Scope(connect,allow_control=True,simulated=True)
    return sim,scope,Advanced(scope,Artifacts(tmp_path/'screenshots')),connections


def test_workflow_restores_running_state_and_uses_one_session(rig):
    sim,scope,a,connections=rig
    sim.trigger='RUN'
    before=settings_fingerprint(sim);sim.commands.clear()
    r=a.control('decode.capture',{'protocol':'uart','bit_rate':10000,'expected_hex':'55 A5 00 FF 52 69 67 6F 6C 0A'})
    assert r['ok'] and r['quality']['status']=='decoded' and r['quality']['expected_sequence_seen']
    assert r['event_count']==10 and r['restoration']['ok']
    assert sim.trigger=='RUN' and not differences(before,settings_fingerprint(sim))
    assert len(connections)==1
    assert not any(c.startswith(':DEC') or c.startswith(':ETAB') for c in sim.commands)
    page=a.read('decode.result',{'artifact_id':r['artifact_id'],'start':3,'count':2})
    assert [e['data'] for e in page['events']]==[255,82] and page['next_start']==5


def test_mismatched_expected_bytes_flag_quality_not_capture(rig):
    _,_,a,_=rig
    r=a.control('decode.capture',{'protocol':'uart','bit_rate':10000,'expected_hex':'DE AD'})
    assert r['ok'] and r['quality']['status']=='suspect' and r['quality']['expected_sequence_seen'] is False


def test_timeout_restores_and_publishes_no_decode(rig):
    sim,_,a,_=rig;sim.single_polls_remaining=None
    before=settings_fingerprint(sim)
    r=a.control('decode.capture',{'protocol':'uart','bit_rate':10000,'timeout_seconds':.2})
    assert not r['ok'] and r['restoration']['ok'] and 'artifact_id' not in r
    assert sim.trigger=='STOP' and not differences(before,settings_fingerprint(sim))


def test_failure_after_mutation_restores_original(rig):
    sim,_,a,_=rig
    original=sim.write
    once=[False]
    def write(command):
        if command.startswith(':TIM:MAIN:OFFS ') and not once[0]:
            once[0]=True
            raise RuntimeError('Injected offset failure')
        original(command)
    sim.write=write
    before=settings_fingerprint(sim)
    r=a.control('decode.capture',{'protocol':'uart','bit_rate':10000})
    assert not r['ok'] and r['restoration']['ok'] and 'Injected' in r['problem']
    assert not differences(before,settings_fingerprint(sim))


@pytest.mark.parametrize('issue',['disabled','armed','unsupported'])
def test_preflight_preserves_instrument(rig,issue):
    sim,scope,a,_=rig
    if issue=='disabled':scope.allow_control=False
    elif issue=='armed':sim.trigger='WAIT';sim.single_polls_remaining=None
    else:sim.settings[':TRIG:MODE']='PULS'
    sim.commands.clear()
    with pytest.raises((PermissionError,ValueError)):
        a.control('decode.capture',{'protocol':'uart','bit_rate':10000})
    assert all(c.endswith('?') for c in sim.commands)


def test_restore_failure_is_not_reported_as_success(rig):
    sim,_,a,_=rig
    original=sim.write
    def write(command):
        if command==':TIM:MAIN:SCAL 0.001':raise RuntimeError('Injected restore failure')
        original(command)
    sim.write=write
    r=a.control('decode.capture',{'protocol':'uart','bit_rate':10000})
    assert not r['ok'] and not r['restoration']['ok'] and r['setup_artifact_id']


def test_saved_result_integrity_checked(rig):
    _,_,a,_=rig
    r=a.control('decode.capture',{'protocol':'uart','bit_rate':10000})
    path=a.bus_results.root/(r['artifact_id']+'.json')
    record=json.loads(path.read_text());record['sha256']='0'*64;path.write_text(json.dumps(record))
    with pytest.raises(ValueError,match='integrity'):a.read('decode.result',{'artifact_id':r['artifact_id']})


def test_small_trigger_rounding_reported_not_retried(rig):
    sim,_,a,_=rig
    original=sim.write
    def write(command):
        original(':TRIG:EDGE:LEV 1.64' if command==':TRIG:EDGE:LEV 1.65' else command)
    sim.write=write
    r=a.control('decode.capture',{'protocol':'uart','bit_rate':10000})
    assert r['ok'] and r['trigger_level_volts']==1.64 and r['trigger_quantization']


@pytest.mark.parametrize('protocol', ['uart', 'spi', 'i2c'])
@pytest.mark.parametrize('duration,rate,depth', [
    (.01, 15625, 6000), (.01, 15626, 60000),
    (.1, 15625, 60000), (.1, 15626, 600000), (.1, 156250, 600000)])
def test_planner_uses_executable_dual_channel_depths(protocol, duration, rate, depth, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    selected = plan({'protocol': protocol, 'window_seconds': duration, 'bit_rate': rate})
    assert selected['depth'] == depth
    sim = Simulator()
    sim.trigger = 'RUN'
    advanced = Advanced(Scope(sim.connect, allow_control=True, simulated=True), Artifacts(tmp_path/'screenshots'))
    result = advanced.control('acquisition.memory.set', {'depth': str(selected['depth'])})
    assert result['ok'] and result['actual']['selected_depth'] == depth
    assert depth >= selected['window_seconds'] * rate * 32


@pytest.mark.parametrize('protocol', ['uart', 'spi', 'i2c'])
@pytest.mark.parametrize('rate', [156251, 200000, 1000000])
def test_unsupported_depth_rejected_before_connection_or_mutation(rig, protocol, rate):
    sim, _, advanced, connections = rig
    sim.commands.clear()
    with pytest.raises(ValueError, match='shorten window_seconds'):
        advanced.control('decode.capture', {'protocol': protocol, 'bit_rate': rate, 'window_seconds': .1})
    assert not sim.commands and not connections


@pytest.mark.parametrize('restore_fails', [False, True])
@pytest.mark.parametrize('storage_failure', ['write', 'readback'])
def test_gateway_preserves_recovery_when_result_storage_fails(rig, monkeypatch, restore_fails, storage_failure):
    import asyncio
    from rigol_mcp.server import build_server
    from rigol_mcp.workflows import SetupStore
    sim, scope, advanced, _ = rig
    sim.trigger = 'RUN'
    if restore_fails:
        write = sim.write
        def fail_restore(command):
            if command == ':TIM:MAIN:SCAL 0.001':
                raise RuntimeError('Injected restoration failure')
            write(command)
        sim.write = fail_restore
    method = 'put' if storage_failure == 'write' else 'load'
    original = getattr(SetupStore, method)
    def fail_storage(store, *args, **kwargs):
        if store.root.name == 'bus-results':
            raise OSError(28, 'Injected result storage failure')
        return original(store, *args, **kwargs)
    monkeypatch.setattr(SetupStore, method, fail_storage)
    server = build_server(scope, advanced.artifacts)
    response = asyncio.run(server.call_tool('advanced_control', {
        'operation': 'decode.capture', 'arguments': {'protocol': 'uart', 'bit_rate': 10000}}))
    result = response.structured_content
    assert response.is_error and not result['ok']
    assert result['persistence']['ok'] is False and 'storage failure' in result['persistence']['problem']
    assert result['restoration']['ok'] is not restore_fails
    assert result['setup_artifact_id'] and result['waveform_artifact_ids']['data']
    assert result['events'] and result['next_start'] is None and 'artifact_id' not in result
    assert sim.commands.count(':SING') == 1
    assert advanced.read('setup.info', {'artifact_id': result['setup_artifact_id']})['ok']
    if restore_fails:
        assert 'Injected restoration failure' in result['restoration']['problem']
        assert 'Inspect restoration failure' in result['next_action']
    else:
        assert sim.timebase == .001 and sim.trigger == 'RUN'
