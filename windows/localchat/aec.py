"""Software acoustic echo canceller (AEC) for the Windows client.

The peer's voice is played through the PC speakers, and a desktop microphone
picks that sound up again, so the peer hears their own voice echoing back
(and the PC user hears a hollow room tone). Windows provides no guaranteed AEC
for the raw capture streams used here (PortAudio/sounddevice and
QtMultimedia), so the echo is cancelled in software:

* The far-end reference is exactly the PCM that is being fed to the speaker.
* The speaker -> microphone path is modelled as a delay plus an adaptive FIR
  filter (normalized LMS / NLMS).
* The delay is re-estimated periodically by envelope cross-correlation, and
  adaptation is frozen during double-talk (the user speaking) so near-end
  speech is never cancelled.

Everything operates in the 16 kHz wire domain (device-rate resampling happens
outside this module). Until the echo path has been identified — or whenever
the estimate looks unreliable — the microphone signal passes through
unchanged, so the AEC can never be worse than no AEC.

This module deliberately imports only numpy so it can be unit-tested without a
Qt/audio backend.
"""

from __future__ import annotations

import threading

import numpy as np

_ENV_DEC = 8  # one envelope sample = 8 PCM samples (0.5 ms at 16 kHz)
_ENV_MIC_WIN = 500  # mic search window in envelope samples (250 ms)
_ENV_HIST = 4000  # reference envelope history (2 s at 16 kHz)
_MIN_SCORE = 0.25  # min normalized correlation to trust a delay candidate


class Aec:
    """Adaptive echo canceller operating on 16 kHz PCM16 mono bytes."""

    def __init__(
        self,
        sample_rate: int = 16000,
        filter_ms: int = 64,
        history_s: float = 2.0,
        delay_search_blocks: int = 25,
    ):
        self.sr = sample_rate
        self.taps = int(sample_rate * filter_ms / 1000)
        self.history = int(sample_rate * history_s)
        self.w = np.zeros(self.taps, dtype=np.float32)
        self.ref = np.zeros(self.history + self.taps, dtype=np.float32)
        self.ref_filled = 0
        self.delay = 0  # samples between the played reference and its echo
        self.confident = False
        self.mu = 0.25
        self._blocks = 0
        self._search_every = max(1, delay_search_blocks)
        self._last_score = 0.0
        self.ref_env: list[float] = []
        self.mic_env: list[float] = []
        # short-term per-sample powers (EMA) used to gate the delay search
        self._ref_p = 0.0
        self._mic_p = 0.0
        # add_reference (playback callback) and process (capture callback) run
        # on different threads with sounddevice; the lock keeps the shared
        # numpy state consistent (each call is ~1 ms, well inside the budget)
        self._lock = threading.Lock()

    # ------------------------------------------------------------- reference

    def add_reference(self, pcm: bytes) -> None:
        """Feed the exact PCM handed to the speaker (16 kHz PCM16 mono)."""
        if not pcm:
            return
        with self._lock:
            x = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
            n = len(x)
            if n >= self.ref.size:
                self.ref[:] = 0.0
                self.ref[-n:] = x
                self.ref_filled = self.ref.size
            else:
                self.ref[:-n] = self.ref[n:]
                self.ref[-n:] = x
                self.ref_filled = min(self.ref_filled + n, self.ref.size)
            p = float(np.dot(x, x)) / n
            self._ref_p = self._ref_p * 0.9 + p * 0.1
            self._append_env(self.ref_env, x)

    # ------------------------------------------------------------- capture

    def process(self, pcm: bytes) -> bytes:
        """Echo-cancel one microphone chunk; returns 16 kHz PCM16 mono bytes
        (identical to the input until the echo path has been identified)."""
        if not pcm:
            return pcm
        with self._lock:
            mic = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
            m = len(mic)
            self._append_env(self.mic_env, mic)
            p_mic = float(np.dot(mic, mic)) / m
            self._mic_p = self._mic_p * 0.9 + p_mic * 0.1
            self._blocks += 1
            # Only re-estimate the delay when the mic looks echo-dominated
            # (quieter than the far-end reference): near-end speech must never
            # be able to steer the delay onto a wrong lag.
            if (
                self._blocks % self._search_every == 0
                and self._mic_p < 2.0 * self._ref_p + 1e-9
            ):
                self._search_delay()
            if not self.confident:
                return pcm

            start = self.ref.size - self.delay - self.taps - m + 1
            if start < 0:
                return pcm
            ref_tail = self.ref[start : start + self.taps + m]
            X = np.lib.stride_tricks.sliding_window_view(ref_tail, self.taps)[:m]
            y = X @ self.w
            e = mic - y

            p_echo = float(np.dot(y, y))
            p_mic = float(np.dot(mic, mic))
            ref_aligned = ref_tail[self.taps - 1 : self.taps - 1 + m]
            p_ref = float(np.dot(ref_aligned, ref_aligned))
            ref_silent = p_ref < 1e-7

            # Double-talk guard: adapt when the mic is mostly echo — either
            # the predicted echo already explains it, or the mic is quieter
            # than the far-end reference (an echo-dominated window). Near-end
            # speech at comparable volume freezes adaptation, and once frozen
            # the next echo-only window resumes learning (no deadlock).
            adapt = p_mic < 4.0 * p_echo + 1e-6 or p_mic < p_ref
            if adapt and not ref_silent:
                XtX = np.einsum("ij,ij->j", X, X) + 1e-6
                self.w += self.mu * (X.T @ e) / XtX
                # divergence guard: an unstable path (e.g. wrong delay) would
                # blow the filter up; reset and re-search instead of corrupting
                # audio
                if float(np.dot(self.w, self.w)) > self.taps * 4.0:
                    self.w[:] = 0.0
                    self.confident = False

            out = (np.clip(e, -1.0, 1.0) * 32767.0).astype(np.int16)
            return out.tobytes()

    # ------------------------------------------------------------- internals

    @staticmethod
    def _append_env(buf: list, x: np.ndarray) -> None:
        """Append short-time energy per _ENV_DEC samples (the envelope)."""
        nblocks = len(x) // _ENV_DEC
        if nblocks == 0:
            return
        e = (x[: nblocks * _ENV_DEC].reshape(nblocks, _ENV_DEC).astype(np.float32) ** 2).mean(
            axis=1
        )
        buf.extend(float(v) for v in e)
        if len(buf) > _ENV_HIST:
            del buf[: len(buf) - _ENV_HIST]

    def _search_delay(self) -> None:
        """Cross-correlate the mic envelope against the reference envelope to
        (re)estimate the speaker->mic delay. Sticky: a new candidate is only
        accepted when it is clearly better than the current estimate."""
        if self.ref_filled < self.history // 2:
            return
        r = np.asarray(self.ref_env, dtype=np.float32)
        mv = np.asarray(self.mic_env, dtype=np.float32)
        L = _ENV_MIC_WIN
        if len(r) < L + 100 or len(mv) < L:
            return
        mic_w = mv[-L:]
        max_lag = len(r) - L
        if max_lag < 100:
            return
        # ref_wins[k] = the reference envelope window that precedes the mic
        # window by k envelope samples (k * _ENV_DEC PCM samples)
        idx = np.arange(L, dtype=np.int64)[None, :] + (
            len(r) - L - np.arange(max_lag + 1, dtype=np.int64)
        )[:, None]
        ref_wins = r[idx]
        mic_norm = float(np.linalg.norm(mic_w)) + 1e-9
        row_norms = np.linalg.norm(ref_wins, axis=1) + 1e-9
        scores = (ref_wins @ mic_w) / (row_norms * mic_norm)
        lag = int(np.argmax(scores))
        best = float(scores[lag])
        if best < _MIN_SCORE:
            return
        # reject ambiguous peaks (periodic envelopes / residual near-end)
        second = float(np.partition(scores, -2)[-2])
        if best < second * 1.1:
            return
        candidate = lag * _ENV_DEC
        if self.confident:
            # keep tracking small drift; jump only when clearly better
            if abs(candidate - self.delay) <= self.taps // 4:
                self.delay = candidate
            elif best > self._last_score * 1.2:
                self.delay = candidate
                self._last_score = best
        else:
            self.delay = candidate
            self._last_score = best
            self.confident = True
