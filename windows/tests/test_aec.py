"""Headless unit tests for the software AEC (no Qt / audio backend needed)."""

import numpy as np
import pytest

from localchat.aec import Aec

SR = 16000
BLOCK = 320  # 20 ms


def _modulated_noise(rng, n, f=3.7, base=0.3, amp=0.7):
    """White noise with a bursty 3.7 Hz envelope, so the envelope has the
    structure a real voice has (needed for delay estimation)."""
    t = np.arange(n) / SR
    gate = (np.sin(2 * np.pi * f * t) > 0).astype(np.float32)
    mod = base + amp * gate
    return rng.normal(0.0, 1.0, n).astype(np.float32) * mod


def _to_pcm(x):
    """float32 [-1, 1] -> int16 PCM bytes."""
    return (np.clip(x, -1.0, 1.0).astype(np.float32) * 32767.0).astype(np.int16).tobytes()


def _run_raw(aec, ref, mic):
    """Feed the reference and the mic in 20 ms blocks, lockstep; returns the
    raw int16 PCM bytes produced by the AEC."""
    out = bytearray()
    n = len(ref)
    for i in range(0, n, BLOCK):
        aec.add_reference(_to_pcm(ref[i : i + BLOCK]))
        out += aec.process(_to_pcm(mic[i : i + BLOCK]))
    return bytes(out)


def _run(aec, ref, mic):
    return np.frombuffer(_run_raw(aec, ref, mic), np.int16).astype(np.float32) / 32768.0


def _echo_power(sig, ref, start, end):
    """Power of the component of `sig` that is correlated with `ref` (the echo)."""
    s = sig[start:end].astype(np.float64)
    r = ref[start:end].astype(np.float64)
    rr = float(np.dot(r, r))
    if rr < 1e-12:
        return 0.0
    return float(np.dot(s, r) ** 2 / rr)


def _corr(a, b, start, end):
    a = a[start:end].astype(np.float64)
    b = b[start:end].astype(np.float64)
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def test_echo_suppressed_after_convergence():
    """A delayed, attenuated copy of the reference in the mic must be removed."""
    rng = np.random.default_rng(42)
    n = SR * 6
    ref = _modulated_noise(rng, n) * 0.3
    delay = 4000  # 250 ms speaker->mic path
    echo = np.zeros(n, np.float32)
    echo[delay:] = ref[: n - delay] * 0.5
    mic = echo + rng.normal(0.0, 0.01, n).astype(np.float32)

    aec = Aec()
    out = _run(aec, ref, mic)

    assert aec.confident, "delay should have been identified"
    before = _echo_power(mic, ref, SR, 2 * SR)
    after = _echo_power(out, ref, 4 * SR, 5 * SR)
    # >13 dB of echo suppression once the filter has converged
    assert after < before * 0.05, f"echo residual too high: {after:.3e} vs {before:.3e}"


def test_near_end_speech_is_not_cancelled():
    """When the user talks, adaptation freezes and their voice survives."""
    rng = np.random.default_rng(7)
    n = SR * 6
    ref = _modulated_noise(rng, n, f=3.7) * 0.3
    delay = 4000
    echo = np.zeros(n, np.float32)
    echo[delay:] = ref[: n - delay] * 0.5
    near = _modulated_noise(rng, n, f=5.1) * 0.5  # the user's voice
    mic = echo + near + rng.normal(0.0, 0.01, n).astype(np.float32)

    aec = Aec()
    out = _run(aec, ref, mic)

    assert _corr(out, near, 4 * SR, 5 * SR) > 0.5, "near-end speech was cancelled"


def test_passthrough_until_delay_identified():
    """Before the echo path is known, the mic must pass through unchanged."""
    rng = np.random.default_rng(3)
    n = SR * 6
    ref = _modulated_noise(rng, n) * 0.3
    delay = 4000
    echo = np.zeros(n, np.float32)
    echo[delay:] = ref[: n - delay] * 0.5
    mic = echo + rng.normal(0.0, 0.01, n).astype(np.float32)

    aec = Aec()
    outb = _run_raw(aec, ref, mic)

    # confidence is reached around 1 s; the first 0.5 s must be byte-identical
    expected = b"".join(_to_pcm(mic[i : i + BLOCK]) for i in range(0, SR // 2, BLOCK))
    assert outb[:SR] == expected


def test_learning_resumes_after_near_end_period():
    """When the user talks first and goes quiet, echo learning must still kick
    in afterwards (previously adaptation could deadlock after warmup)."""
    rng = np.random.default_rng(11)
    n = SR * 8
    ref = _modulated_noise(rng, n, f=3.7) * 0.3
    delay = 4000
    echo = np.zeros(n, np.float32)
    echo[delay:] = ref[: n - delay] * 0.5
    near = np.zeros(n, np.float32)
    near[: 2 * SR] = _modulated_noise(rng, n, f=5.1)[: 2 * SR] * 0.5  # user talks first
    mic = echo + near + rng.normal(0.0, 0.01, n).astype(np.float32)

    aec = Aec()
    out = _run(aec, ref, mic)

    before = _echo_power(mic, ref, 2 * SR, 3 * SR)  # just after the near-end stops
    after = _echo_power(out, ref, 6 * SR, 7 * SR)
    assert after < before * 0.1, f"echo not learned after near-end: {after:.3e} vs {before:.3e}"


def test_concurrent_reference_and_capture():
    """add_reference (playback callback) and process (capture callback) run on
    separate sounddevice threads; they must be safe to call concurrently."""
    import threading

    rng = np.random.default_rng(5)
    n = SR * 2
    ref = _modulated_noise(rng, n) * 0.3
    mic = ref * 0.3 + rng.normal(0.0, 0.01, n).astype(np.float32)
    aec = Aec()
    errors = []

    def play():
        try:
            for i in range(0, n, BLOCK):
                aec.add_reference(_to_pcm(ref[i : i + BLOCK]))
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    def cap():
        try:
            for i in range(0, n, BLOCK):
                aec.process(_to_pcm(mic[i : i + BLOCK]))
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    t1 = threading.Thread(target=play)
    t2 = threading.Thread(target=cap)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors


def test_silent_reference_is_passthrough():
    """Adaptation on silence must do nothing (never blow up the signal)."""
    n = SR * 3
    ref = np.zeros(n, np.float32)
    t = np.arange(n) / SR
    mic = (np.sin(2 * np.pi * 440 * t) * 0.1).astype(np.float32)

    aec = Aec()
    out = _run(aec, ref, mic)

    expected = np.frombuffer(_to_pcm(mic), np.int16).astype(np.float32) / 32768.0
    assert np.max(np.abs(out - expected)) < 1e-4
