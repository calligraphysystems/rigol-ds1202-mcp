# TinyPICO known-signal fixture

`tinypico_fixture.py` runs on a TinyPICO with board-specific MicroPython. It is
inert on import; bounded `run(mode, seconds)` releases pins on exit. No automatic
startup script is installed. Use a bare test board, not a connected application bus.
Back up your own existing firmware before any replacement. Firmware is not bundled.
Get compatible firmware/instructions from the [official TinyPICO page](https://micropython.org/download/UM_TINYPICO/).
The tested fixture used MicroPython 1.29.0 on ESP32-PICO-D4.

| Probe | Connection |
| --- | --- |
| CH1, physical 10x | GPIO22, clock |
| CH2, physical 10x | GPIO21, data/UART TX |
| Probe grounds | Board GND; short local connections preferred |

USB supplies power. Leave GPIO19 unconnected. Copy the module using your selected
MicroPython tool/serial port; keep the controlling session attached during a run.

```python
import tinypico_fixture as fixture
print(fixture.info())
fixture.run("uart", 60)
```

Modes accept 1–600 seconds. UART: 9600 baud/8N1, repeating
`55 A5 00 FF 52 69 67 6F 6C 0A`. SPI: nominal 10 kHz/mode0/8-bit/MSB,
`A5 5A 00 FF`, one byte per frame with 10 ms gaps. I2C: slow software clock,
7-bit address `50`, write data `12 A5 5A`, synthetic ACKs from this same board.
I2C uses internal pull-ups and is not a real slave or electrical-compliance test.

During the fixture run, call `decode.capture` with the matching protocol,
`data_channel:2`, `probe_ratio:10`, `logic_voltage:3.3`, `window_seconds:0.1`.
Use `bit_rate:9600` for UART, `10000` for SPI, `4000` as the tested I2C maximum
clock bound; set `clock_channel:1` for SPI/I2C only. For SPI use
`frame_gap_seconds:0.001`. Supply the corresponding `expected_hex` when desired.
Inspect all result pages and restoration, not merely `ok`. Rate is supplied,
not detected. Unknown wiring/voltage must be resolved physically before testing.

The historical final test used one ground clip on CH2/GND; CH1 shared scope ground.
Data patterns passed but high-frequency edge fidelity was not established.
Wide-view scope hardware tables gave wrong SPI/UART values; host decode.capture
avoids that observed table limitation. These results do not validate all rates.
