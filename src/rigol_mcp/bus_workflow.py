"""Capture once, decode full-resolution samples, restore the prior scope setup."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import numpy as np

from .scope import Scope
from .workflows import idle_for_control, restore_plan, settings_fingerprint, differences
from .memory import capture_memory, MEMORY_DEPTHS
from .serial_decode import decode

CAPTURE_MAX_POINTS = 600000

def plan(arguments):
    protocol = arguments['protocol']
    rate = arguments['bit_rate']
    duration = arguments.get('window_seconds', .05)
    scales = sorted(v*10**e for e in range(-9,2) for v in (1,2,5))
    scale = next(v for v in scales if 12*v >= duration*(1-1e-12))
    points = math.ceil(12*scale*rate*32)
    # This workflow always enables both channels, including for UART. Keep its
    # bounded allocation below the next legal dual-channel depth (6M points).
    depth = next((n for n in MEMORY_DEPTHS[2] if points <= n <= CAPTURE_MAX_POINTS), None)
    if depth is None:
        maximum_window = max(12*v for v in scales if math.ceil(12*v*rate*32) <= CAPTURE_MAX_POINTS)
        raise ValueError(f'Request needs an unsupported capture depth; this workflow uses at most '
                         f'600000 points per channel. For bit_rate={rate:g}, shorten '
                         f'window_seconds to <= {maximum_window:g} (including timebase rounding).')
    data = arguments.get('data_channel',2)
    clock = arguments.get('clock_channel',1)
    if protocol != 'uart' and data == clock:
        raise ValueError('Clock and data must use distinct channels')
    if protocol == 'uart' and 'clock_channel' in arguments:
        raise ValueError('UART uses only data_channel; omit clock_channel')
    gap = arguments.get('frame_gap_seconds',16/rate)
    if protocol != 'spi' and 'frame_gap_seconds' in arguments:
        raise ValueError('frame_gap_seconds is only applicable to SPI')
    if protocol == 'spi' and (gap <= 2/rate or 12*scale < 2*gap+8/rate):
        raise ValueError('SPI needs a gap >2 bit periods and a window covering the word plus two gaps')
    expected = arguments.get('expected_hex')
    if expected is not None:
        try:
            expected = bytes.fromhex(expected)
        except ValueError as exc:
            raise ValueError('expected_hex must contain hexadecimal byte pairs') from exc
        if not 1 <= len(expected) <= 256:
            raise ValueError('expected_hex must contain 1..256 bytes')
    return {'protocol':protocol,'bit_rate':rate,'seconds_per_division':scale,
            'window_seconds':12*scale,'depth':depth,'data_channel':data,'clock_channel':clock,
            'logic_voltage':arguments.get('logic_voltage',3.3),'probe_ratio':arguments.get('probe_ratio',10),
            'frame_gap_seconds':gap,'expected':expected}


def event_page(events, start=0, count=20):
    if start > len(events):
        raise ValueError('start exceeds the number of stored events')
    end = start
    used = 0
    for event in events[start:start+count]:
        size = len(json.dumps(event))
        if used+size > 16000:
            break
        end += 1
        used += size
    return {'start':start,'events':events[start:end],
            'next_start':end if end<len(events) else None}


def result_page(store, artifact_id, start=0, count=20):
    meta, raw = store.load(artifact_id)
    return {**meta, **event_page(json.loads(raw)['events'], start, count)}


def publish_result(store, result, events, identity):
    """Persistence must never replace capture/restoration diagnostics."""
    try:
        metadata = store.put(json.dumps({'events': events}).encode(),
                             {**result, 'artifact_type': 'serial.decode', 'instrument': identity,
                              'created_at_utc': datetime.now(timezone.utc).isoformat()})
        return result_page(store, metadata['artifact_id'])
    except Exception as exc:
        # The in-memory first page is still useful, but promise neither an artifact
        # nor further pages when saving or validating the saved result failed.
        page = event_page(events)
        return {**result, **page, 'ok': False, 'next_start': None,
                'persistence': {'ok': False, 'problem': str(exc)},
                'events_truncated': page['next_start'] is not None,
                'next_action': ('Inspect restoration failure before further control; use setup_artifact_id to recover. '
                                if not result['restoration'].get('ok', False) else 'Prior core settings restored. ')
                               + 'Result persistence failed; only this event preview is available. '
                                 'Preserve the returned evidence and resolve storage before a new capture; do not automatically retry.'}


def capture(advanced, **arguments):
    # Validate all dependent choices before acquiring the instrument or sending writes.
    selected = plan(arguments)
    original_scope = advanced.scope
    result = {'ok':False,'simulated':original_scope.simulated,'protocol':selected['protocol'],
              'method':'host decoding of full-resolution saved samples',
              'settings':{k:v for k,v in selected.items() if k!='expected'},
              'restoration':{},'waveform_artifact_ids':{},'quality':{'status':'unavailable','flags':[]}}
    with original_scope.session(control=True) as (io, identity):
        idle_for_control(io)
        original_scope._check_error(io,'before capture-and-decode')
        saved = advanced.setups.save(io,identity,original_scope.simulated)
        restore_plan(saved)  # Reject unsupported modes before the first setting changes.
        result['setup_artifact_id'] = saved['artifact_id']

        # Nested operations reuse the ONE already-held instrument lock and connection.
        @contextmanager
        def borrowed_connection():
            yield io
        scope = Scope(borrowed_connection,allow_control=True,simulated=original_scope.simulated)
        from .advanced import Advanced
        nested = Advanced(scope,advanced.artifacts)
        mutated = False
        decoded = None
        def checked(value):
            if not value.get('ok',True) or value.get('readback_differences'):
                raise RuntimeError(value.get('problem') or 'Requested settings not confirmed: '+str(value.get('readback_differences')))
            return value
        def control(op, **args):
            return checked(nested.control(op,args))
        try:
            mutated = True
            # AUTO sweep prevents an existing stopped SINGLE setup from rearming implicitly.
            control('trigger.edge.set',sweep='AUTO')
            scope.acquisition('run')
            control('acquisition.memory.set',depth='AUTO')
            volts = selected['logic_voltage']
            vertical = next(v for v in sorted(m*10**e for e in range(-3,2) for m in (1,2,5)) if v>=volts/5)
            for channel in (1,2):
                control('channel.set',channel=channel,enabled=True,probe_ratio=selected['probe_ratio'],
                        fine_scale=False,scale_per_division=vertical,offset=-volts/2,
                        coupling='DC',inverted=False,amplitude_unit='VOLT',bandwidth_limit='20M')
            io.write(':TIM:MAIN:OFFS 0')
            scope._check_error(io,'centering temporary capture window')
            checked(scope.set_timebase(selected['seconds_per_division']))
            actual_scale = float(io.query(':TIM:MAIN:SCAL?'))
            if not math.isclose(actual_scale,selected['seconds_per_division'],rel_tol=1e-6):
                raise RuntimeError('Unexpected timebase quantization')
            # Keep 10% pretrigger and 90% posttrigger, enough to observe complete packets.
            offset = 4.8*actual_scale
            io.write(f':TIM:MAIN:OFFS {offset:.12g}')
            scope._check_error(io,'setting post-trigger capture window')
            if not math.isclose(float(io.query(':TIM:MAIN:OFFS?')),offset,rel_tol=1e-5,abs_tol=1e-12):
                raise RuntimeError('Capture window offset not confirmed')
            control('acquisition.memory.set',depth=str(selected['depth']))
            source = selected['data_channel'] if selected['protocol']=='uart' else selected['clock_channel']
            trigger = nested.control('trigger.edge.set',dict(source=f'CHAN{source}',slope='NEG' if selected['protocol']=='uart' else 'POS',
                    level=volts/2,sweep='AUTO',coupling='DC',noise_reject=False,holdoff_seconds=16e-9))
            # The physical scope rounds trigger level in small vertical-scale steps.
            hard = [d for d in trigger.get('readback_differences',[]) if
                    d['field'] != 'level' or abs(d['actual']-d['requested']) > .021*vertical]
            checked({**trigger,'readback_differences':hard})
            result['trigger_level_volts'] = trigger['actual']['level']
            result['trigger_quantization'] = trigger.get('readback_differences',[])
            capture_result = control('acquisition.capture_single',channel=selected['data_channel'],
                                     timeout_seconds=arguments.get('timeout_seconds',5),transfer_timeout_seconds=30)
            result['waveform_artifact_ids']['data'] = capture_result['artifact_id']
            stopped_settings = settings_fingerprint(io)
            if selected['protocol']!='uart':
                clock_result = checked(capture_memory(scope,nested.waveforms,selected['clock_channel'],timeout_seconds=30))
                result['waveform_artifact_ids']['clock'] = clock_result['artifact_id']
            if io.query(':TRIG:STAT?')!='STOP' or differences(stopped_settings,settings_fingerprint(io)):
                raise RuntimeError('Acquisition/settings changed between channel transfers')
            waves = {key:nested.waveforms.read(identifier,payload=True) for key,identifier in result['waveform_artifact_ids'].items()}
            meta, raw = waves['data']
            scaling = meta['scaling']
            grid = (scaling['point_count'],scaling['x_increment'],scaling['x_origin'],scaling['x_reference'])
            logic, signal_flags, levels = {}, [], {}
            for key,(metadata,raw) in waves.items():
                s=metadata['scaling']
                other=(s['point_count'],s['x_increment'],s['x_origin'],s['x_reference'])
                if other != grid:
                    raise RuntimeError('Clock/data sample grids differ; coherent decode refused')
                codes=np.frombuffer(raw,dtype=np.uint8)
                values=(codes.astype(float)-s['y_origin']-s['y_reference'])*s['y_increment']
                low,high=float(values.min()),float(values.max())
                levels[key]={'minimum_volts':low,'maximum_volts':high}
                if np.any(codes==0) or np.any(codes==255):signal_flags.append(key+'_clipped')
                if low > .3*volts or high < .7*volts:signal_flags.append(key+'_missing_logic_levels')
                logic[key]=values>=volts/2
            decoded=decode(selected['protocol'],logic['data'],logic.get('clock'),grid[1],
                           grid[2]-grid[3]*grid[1],selected['bit_rate'],selected['frame_gap_seconds'])
            flags=sorted(set(decoded['flags']+signal_flags))
            flattened=[]
            for event in decoded['events']:
                flattened.extend(event['data'] if isinstance(event['data'],list) else [event['data']])
            expected=selected['expected']
            match = None if expected is None else expected in bytes(flattened)
            if match is False:flags.append('expected_sequence_not_found')
            result['quality']={'status':'no_data' if not decoded['events'] else 'suspect' if flags else 'decoded',
                               'flags':flags,'boundary_fragments':decoded['boundary_fragments'],
                               'samples_per_declared_bit':decoded['samples_per_declared_bit'],
                               'expected_sequence_seen':match,'signal_levels':levels}
            result['event_count']=len(decoded['events'])
            result['limitations']='UART 8N1 normal polarity, requires an observed 10-bit idle interval to establish initial framing; SPI mode 0, 8-bit MSB, timeout framing without CS; I2C 7-bit only. Threshold is half the declared logic voltage. Decoded means no detected structural flags, not electrical compliance or proof of correct unknown payloads. Expected matching searches the ordered decoded data bytes, excluding I2C address/ACK. Boundary fragments are excluded. No instrument decoder/table settings are changed.'
        except Exception as exc:
            result['problem']=str(exc)
        finally:
            if mutated:
                try:
                    # Explicit STOP enables restoration even after a timed-out acquisition.
                    scope.acquisition('stop')
                    restored=nested.control('setup.restore',{'artifact_id':saved['artifact_id'],'acquisition_after':'saved'})
                    result['restoration']={'ok':restored['ok'],'acquisition_after':restored['acquisition_after'],
                                           'readback_differences':restored['readback_differences'],
                                           'completed_writes':restored['completed_writes'],
                                           'rollback_artifact_id':restored['rollback_artifact_id'], 'problem':restored['problem']}
                except Exception as exc:
                    result['restoration']={'ok':False,'problem':str(exc)}
            result['ok']=decoded is not None and not result.get('problem') and result['restoration'].get('ok',False)
            result['next_action']=('Inspect restoration failure before further control; use setup_artifact_id to recover.'
                                   if not result['restoration'].get('ok',False) else
                                   'Inspect quality flags; do not treat these bytes as verified.' if result['quality']['status']!='decoded' else
                                   'Read decode.result only if more than the first 20 events are needed. Prior settings restored; retained waveform data is not restored.')
            if decoded is not None:
                result = publish_result(nested.bus_results, result, decoded['events'], identity)
    return result
