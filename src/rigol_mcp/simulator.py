"""Deterministic simulated scope; no physical instrument is contacted."""

import math
import io
import json
from contextlib import contextmanager
from PIL import Image, ImageDraw


class Simulator:
    def __init__(self):
        self.trigger = "STOP"
        self.timebase = 0.001
        self.commands = []
        self.memory_points = 600001
        self.single_polls_remaining = None
        self.settings = {":TRIG:MODE": "EDGE", ":TRIG:EDGE:SOUR": "CHAN1",
                         ":TRIG:EDGE:SLOP": "POS", ":TRIG:EDGE:LEV": "0",
                         ":TRIG:COUP": "DC", ":TRIG:HOLD": "0.000000016",
                         ":TRIG:NREJ": "0", ":TRIG:SWE": "AUTO",
                         ":ACQ:TYPE": "NORM", ":ACQ:MDEP": "AUTO", ":ACQ:SRAT": "1000000",
                         ":TIM:MAIN:OFFS": "0", ":WAV:SOUR": "CHAN1", ":WAV:MODE": "NORM",
                         ":WAV:FORM": "BYTE", ":WAV:STAR": "1", ":WAV:STOP": "1200",
                         ":FUNC:WREC:ENAB": "0"}
        for channel in (1, 2):
            for suffix, value in {"DISP": "1", "SCAL": "1", "UNIT": "VOLT", "PROB": "10",
                                  "RANG": "8", "OFFS": "0", "BWL": "OFF", "COUP": "DC",
                                  "INV": "0", "VERN": "0", "TCAL": "0"}.items():
                self.settings[f":CHAN{channel}:{suffix}"] = value

        self.event_rows = b"Time,TX\n0.000000,F0\n0.001000,F0\n"
        for decoder in (1, 2):
            defaults = {"MODE": "PAR", "DISP": "0", "FORM": "ASC", "THRE:AUTO": "1",
                "THRE:CHAN1": "0", "THRE:CHAN2": "0", "UART:TX": "CHAN1", "UART:RX": "OFF",
                "UART:BAUD": "9600", "UART:WIDT": "8", "UART:STOP": "1", "UART:PAR": "NONE",
                "UART:POL": "POS", "UART:END": "LSB", "IIC:CLK": "CHAN1", "IIC:DATA": "CHAN2",
                "IIC:ADDR": "NORM", "SPI:CLK": "CHAN1", "SPI:MOSI": "CHAN2", "SPI:MISO": "OFF",
                "SPI:MODE": "TIM", "SPI:EDGE": "RISE", "SPI:WIDT": "8", "SPI:END": "MSB",
                "SPI:POL": "POS", "SPI:TIM": "0.000001"}
            self.settings.update({f":DEC{decoder}:{key}": value for key, value in defaults.items()})
            self.settings.update({f":ETAB{decoder}:{key}": value for key, value in {
                "DISP": "0", "FORM": "HEX", "VIEW": "PACK", "SORT": "ASC", "ROW": "1"}.items()})

    @contextmanager
    def connect(self):
        yield self

    def query(self, command):
        self.commands.append(command)
        if command == ":TRIG:STAT?" and self.trigger == "WAIT" and self.single_polls_remaining is not None:
            if self.single_polls_remaining == 0:
                self.trigger = "STOP"
            else:
                self.single_polls_remaining -= 1
        if command == ":WAV:PRE?" and self.settings[":WAV:MODE"] == "RAW":
            return f"0,2,{self.memory_points},1,0.000001,-0.003,0,0.04,0,127"
        fixed = {"*IDN?": "RIGOL TECHNOLOGIES,DS1202Z-E,SIMULATED,0.0",
                 ":TRIG:STAT?": self.trigger, ":TIM:MODE?": "MAIN",
                 ":TIM:MAIN:SCAL?": str(self.timebase), ":SYST:ERR?": '0,"No error"',
                 ":WAV:PRE?": f"0,0,1200,1,{self.timebase / 100},{-6 * self.timebase},0,0.04,0,127"}
        if command in fixed:
            return fixed[command]
        if command.endswith("?") and command[:-1] in self.settings:
            return self.settings[command[:-1]]
        if command.startswith(":MEAS:ITEM? "):
            item = command.split(" ")[1].split(",")[0]
            return str({"FREQ": 1000, "PER": .001, "VPP": 4, "VRMS": math.sqrt(2),
                        "VAVG": 0, "VMIN": -2, "VMAX": 2, "RTIM": .000295,
                        "FTIM": .000295, "PDUT": 50}[item])
        raise ValueError(f"Unimplemented simulated query: {command}")

    def write(self, command):
        self.commands.append(command)
        if command in (":RUN", ":STOP", ":SING"):
            self.trigger = {":RUN": "RUN", ":STOP": "STOP", ":SING": "WAIT"}[command]
            if command == ":SING":
                self.settings[":TRIG:SWE"] = "SING"
        elif command.startswith(":TIM:MAIN:SCAL "):
            self.timebase = float(command.split()[1])
        elif command.split()[0] in self.settings:
            key, value = command.split()
            if key.endswith(":PROB"):
                prefix = key.rsplit(":", 1)[0]
                ratio = float(value) / float(self.settings[key])
                for suffix in ("SCAL", "OFFS", "RANG"):
                    self.settings[f"{prefix}:{suffix}"] = str(float(self.settings[f"{prefix}:{suffix}"]) * ratio)
            self.settings[key] = value
            if key == ":ACQ:MDEP":
                self.memory_points = 600001 if value == "AUTO" else int(value)
            if key.endswith(":SCAL"):
                self.settings[key.replace(":SCAL", ":RANG")] = str(float(value) * 8)
            if key.endswith(":RANG"):
                self.settings[key.replace(":RANG", ":SCAL")] = str(float(value) / 8)
        elif command not in (":WAV:SOUR CHAN1", ":WAV:SOUR CHAN2", ":WAV:MODE NORM",
                              ":WAV:FORM BYTE", ":WAV:STAR 1", ":WAV:STOP 1200"):
            raise ValueError(f"Unimplemented simulated write: {command}")

    def binary(self, command, maximum):
        if command in (":ETAB1:DATA?", ":ETAB2:DATA?"):
            self.commands.append(command)
            return self.event_rows
        if command == ":SYST:SET?":
            self.commands.append(command)
            return json.dumps({"settings": self.settings, "timebase": self.timebase}).encode()
        if command == ":DISP:DATA? ON,OFF,BMP24":
            self.commands.append(command)
            image = Image.new("RGB", (800, 480), "#151515")
            ImageDraw.Draw(image).text((20, 20), "SIMULATED DS1202Z-E DISPLAY - NOT A REAL MEASUREMENT", fill="yellow")
            buffer = io.BytesIO()
            image.save(buffer, format="BMP")
            data = buffer.getvalue()
            if len(data) > maximum:
                raise ValueError("Simulated screenshot exceeds maximum")
            return data
        if command == ":WAV:DATA?" and self.settings[":WAV:MODE"] == "RAW":
            self.commands.append(command)
            start, end = int(self.settings[":WAV:STAR"]) - 1, int(self.settings[":WAV:STOP"])
            if not 0 <= start < end <= self.memory_points or end - start > min(maximum, 250000):
                raise ValueError("Invalid simulated memory window")
            # A continuous global-index pattern exposes duplicate/omitted chunk boundaries.
            return bytes(i % 256 for i in range(start, end))
        if command != ":WAV:DATA?" or maximum < 1200:
            raise ValueError("Invalid simulated waveform request")
        self.commands.append(command)
        return bytes(round(127 + 50 * math.sin(2 * math.pi * 1000 *
                     (i * self.timebase / 100 - 6 * self.timebase))) for i in range(1200))

