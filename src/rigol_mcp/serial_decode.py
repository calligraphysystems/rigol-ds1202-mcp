"""Bounded host decoding of sampled logic; no instrument access or guessed baud."""
import math
import numpy as np

MAX_EVENTS = 4096


def decode(protocol, data, clock, dt, origin, bit_rate, frame_gap_seconds):
    """Boolean samples on a shared time grid. UART 8N1, SPI mode 0/8/MSB, I2C 7-bit."""
    data = np.asarray(data, dtype=bool)
    clock = None if clock is None else np.asarray(clock, dtype=bool)
    if len(data) < 2 or not math.isfinite(dt) or dt <= 0:
        raise ValueError('Invalid sample grid')
    if protocol != 'uart' and (clock is None or len(clock) != len(data)):
        raise ValueError('Clock and data need the same sample grid')
    if protocol not in ('uart', 'spi', 'i2c'):
        raise ValueError('Unsupported protocol')
    events, flags = [], []
    partial = 0
    samples_per_bit = 1 / (dt * bit_rate)
    if samples_per_bit < 16:
        return {'events': [], 'flags': ['insufficient_sample_rate'], 'boundary_fragments': 0,
                'samples_per_declared_bit': samples_per_bit}
    if clock is not None:
        rising = np.flatnonzero(~clock[:-1] & clock[1:])+1
        if len(rising)>1:
            shortest = int(np.diff(rising).min())
            if shortest < 16:
                flags.append('insufficient_observed_clock_samples')
            if shortest*dt < .8/bit_rate:
                flags.append('clock_faster_than_declared')

    def emit(event):
        if len(events) >= MAX_EVENTS:
            raise ValueError('More than 4096 events; request a shorter capture window')
        events.append(event)

    def when(index):
        return origin + int(index)*dt

    if protocol == 'uart':
        falls = np.flatnonzero(data[:-1] & ~data[1:]) + 1
        next_start = 0
        synchronized = False
        for start in falls:
            if start < next_start:
                continue
            # Establish framing from an observed idle character before the first byte.
            # Once aligned, an ordinary stop bit is enough for contiguous bytes.
            idle_start = int(start - (.5 if synchronized else 10)*samples_per_bit)
            if idle_start < 0 or not np.all(data[idle_start:start]):
                partial += 1
                continue
            last = int(round(start + 9.5*samples_per_bit))
            if last >= len(data):
                partial += 1
                break
            if data[int(round(start + .5*samples_per_bit))]:
                flags.append('short_start_pulse')
                continue
            value = sum(int(data[int(round(start+(1.5+k)*samples_per_bit))]) << k for k in range(8))
            good_stop = bool(data[last])
            emit({'time_seconds':when(start), 'data':value, 'hex':f'{value:02X}', 'stop_valid':good_stop})
            if not good_stop:
                flags.append('uart_stop_error')
            synchronized = good_stop
            next_start = last + 1
        if len(falls) and not events:
            flags.append('uart_idle_boundary_missing_or_incomplete_frame')
    elif protocol == 'spi':
        rises = np.flatnonzero(~clock[:-1] & clock[1:]) + 1
        edges = np.flatnonzero(clock[:-1] != clock[1:]) + 1
        if len(edges) > 1 and np.any(np.diff(edges)*dt < .1/bit_rate):
            flags.append('short_clock_pulse')
        groups = np.split(rises, np.flatnonzero(np.diff(rises)*dt > frame_gap_seconds)+1)
        for group in groups:
            if not len(group):
                continue
            # A full timeout on either side makes the grouping independently observable.
            left = group[0]*dt >= frame_gap_seconds and not np.any(clock[max(0,int(group[0]-frame_gap_seconds/dt)):group[0]])
            right = (len(clock)-1-group[-1])*dt >= frame_gap_seconds
            if not left or not right:
                partial += 1
                continue
            if len(group) % 8:
                flags.append('spi_word_width_error')
                continue
            for start in range(0,len(group),8):
                indices = group[start:start+8]
                # MOSI must be stable around the sampling edge for mode 0.
                margin = max(1,int(.05*samples_per_bit))
                if any(np.any(data[max(0,int(i)-margin):min(len(data),int(i)+margin+1)] != data[i]) for i in indices):
                    flags.append('spi_data_changes_at_sampling_edge')
                value = 0
                for index in indices:
                    value = (value << 1) | int(data[index])
                emit({'time_seconds':when(indices[0]), 'data':value, 'hex':f'{value:02X}'})
    else:
        # START/STOP: data transition while clock remains high on both sides.
        transitions = np.flatnonzero((data[:-1] != data[1:]) & clock[:-1] & clock[1:]) + 1
        rises = np.flatnonzero(~clock[:-1] & clock[1:]) + 1
        active = None
        for index in transitions:
            start_condition = not data[index]
            if active is not None:
                selected = rises[(rises > active) & (rises < index)]
                # The final SCL rise used to form STOP/repeated START is not a data bit.
                if len(selected) % 9 == 1:
                    selected = selected[:-1]
                if not len(selected) or len(selected) % 9:
                    flags.append('i2c_byte_width_error')
                else:
                    if len(selected) > 9*257:
                        raise ValueError('I2C transaction exceeds 256 data bytes; shorten capture window')
                    words, acks = [], []
                    for offset in range(0,len(selected),9):
                        value = 0
                        for sample in selected[offset:offset+8]:
                            value = (value << 1) | int(data[sample])
                        words.append(value)
                        acks.append(not bool(data[selected[offset+8]]))
                    if words[0] & 0xF8 == 0xF0:
                        flags.append('i2c_10bit_address_unsupported')
                    else:
                        emit({'time_seconds':when(active), 'address':words[0] >> 1,
                              'direction':'read' if words[0]&1 else 'write', 'data':words[1:],
                              'hex':' '.join(f'{b:02X}' for b in words[1:]), 'acknowledged':acks,
                              'end':'repeated_start' if start_condition else 'stop'})
            elif not start_condition:
                partial += 1
            active = int(index) if start_condition else None
        if active is not None:
            partial += 1
    return {'events':events, 'flags':sorted(set(flags)), 'boundary_fragments':partial,
            'samples_per_declared_bit':samples_per_bit}
