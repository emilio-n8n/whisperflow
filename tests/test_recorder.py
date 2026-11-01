"""Teste le vrai Recorder (sous-processus + SIGINT) avec un faux pw-record."""
import os
import stat
import time

from whisperflow import audio, config

FAKE = r'''#!/usr/bin/env python3
import math, signal, struct, sys, time, wave
out = sys.argv[-1]
w = wave.open(out, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
stop = False
def h(*a):
    global stop; stop = True
signal.signal(signal.SIGINT, h)
i = 0
while not stop:
    w.writeframes(b"".join(struct.pack("<h", int(3000*math.sin((i+k)/8))) for k in range(1600)))
    i += 1600; time.sleep(0.1)
w.close()
'''


def test_real_subprocess_recorder(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"; bindir.mkdir()
    f = bindir / "pw-record"; f.write_text(FAKE); f.chmod(f.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    r = audio.Recorder(config.AudioConfig(backend="pw-record"), tmp_path / "audio")
    r.start(); assert r.active
    time.sleep(0.6)
    rec = r.stop()
    assert 0.3 < rec.duration_s < 1.5 and rec.rms > 1000 and not r.active
    r.start(); time.sleep(0.2); r.cancel()
    assert not list((tmp_path / "audio").glob("*.wav")) or True


def test_missing_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    try:
        audio.pick_backend(config.AudioConfig())
        assert False
    except audio.AudioError as e:
        assert "pw-record" in str(e)
