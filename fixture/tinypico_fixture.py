"""Known serial waveforms for a bare TinyPICO + passive scope probes.

Loaded on demand; does not auto-start. No external bus devices should be attached.
I2C ACKs are deliberately generated here: this is a decoder test stimulus, not
an I2C master talking to a real slave. Internal pull-ups suit the deliberately
slow short-wire fixture; inspect rise time before claiming electrical compliance.
"""
from machine import Pin, UART, SoftSPI
import gc
import time

CLOCK_PIN = 22
DATA_PIN = 21
UNUSED_INPUT_PIN = 19
UART_BYTES = b"\x55\xa5\x00\xffRigol\n"
SPI_BYTES = b"\xa5\x5a\x00\xff"
I2C_ADDRESS = 0x50
I2C_BYTES = b"\x12\xa5\x5a"


def info():
    return {"fixture": "rigol-tinypico-v1", "clock_gpio": CLOCK_PIN,
            "data_gpio": DATA_PIN, "ground": "GND", "uart_baud": 9600,
            "uart_hex": UART_BYTES.hex(), "spi_hex": SPI_BYTES.hex(),
            "spi_mode": 0, "spi_nominal_clock_hz": 10000,
            "i2c_7bit_address": I2C_ADDRESS, "i2c_hex": I2C_BYTES.hex(),
            "i2c_ack": "synthetic, driven by this fixture", "running": False}


def release():
    Pin(CLOCK_PIN, Pin.IN)
    Pin(DATA_PIN, Pin.IN)
    Pin(UNUSED_INPUT_PIN, Pin.IN)


def i2c_frame(clock, data, delay_us=100):
    def wait():
        time.sleep_us(delay_us)
    # START followed by address-write, three data bytes and generated ACK bits.
    data(1); clock(1); wait()
    data(0); wait(); clock(0); wait()
    for byte in bytes((I2C_ADDRESS << 1,)) + I2C_BYTES:
        for shift in range(7, -1, -1):
            data((byte >> shift) & 1); wait()
            clock(1); wait(); clock(0); wait()
        data(0); wait()
        clock(1); wait(); clock(0); wait()
    # STOP, then release both lines.
    data(0); wait(); clock(1); wait(); data(1); wait()


def run(mode, seconds=300):
    """Run a selected stimulus for at most ten minutes, then release the pins."""
    if mode not in ("uart", "i2c", "spi"):
        raise ValueError("mode must be uart, i2c or spi")
    if not isinstance(seconds, int) or not 1 <= seconds <= 600:
        raise ValueError("seconds must be 1..600")
    release()
    peripheral = None
    frames = 0
    try:
        if mode == "uart":
            peripheral = UART(1, baudrate=9600, bits=8, parity=None, stop=1,
                              tx=Pin(DATA_PIN), rx=Pin(UNUSED_INPUT_PIN))
        elif mode == "spi":
            peripheral = SoftSPI(baudrate=10000, polarity=0, phase=0,
                                 sck=Pin(CLOCK_PIN), mosi=Pin(DATA_PIN), miso=Pin(UNUSED_INPUT_PIN))
        else:
            clock = Pin(CLOCK_PIN, Pin.OPEN_DRAIN, Pin.PULL_UP, value=1)
            data = Pin(DATA_PIN, Pin.OPEN_DRAIN, Pin.PULL_UP, value=1)
            time.sleep_ms(10)
            if not clock.value() or not data.value():
                raise RuntimeError("I2C lines held low; check wiring and pull-ups")
        started = time.ticks_ms()
        print("FIXTURE_READY", mode, seconds)
        while time.ticks_diff(time.ticks_ms(), started) < seconds * 1000:
            if mode == "i2c":
                gc.collect()
                gc.disable()
                try:
                    i2c_frame(clock, data)
                finally:
                    gc.enable()
                time.sleep_ms(30)
            elif mode == "spi":
                # One 8-bit word per timeout-delimited frame, repeated known sequence.
                for index in range(len(SPI_BYTES)):
                    peripheral.write(SPI_BYTES[index:index + 1])
                    time.sleep_ms(10)
            else:
                peripheral.write(UART_BYTES)
                time.sleep_ms(30)
            frames += 1
        return {"mode": mode, "sequences_sent": frames, "elapsed_ms": time.ticks_diff(time.ticks_ms(), started)}
    finally:
        try:
            if peripheral is not None:
                peripheral.deinit()
        finally:
            release()
            print("FIXTURE_STOPPED", mode)
