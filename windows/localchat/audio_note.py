"""Voice-message capture and playback (local-only, no protocol code).

Both ends exchange plain 16 kHz mono 16-bit PCM WAV files (sent through the
ordinary file channel), so no codec and no extra Qt multimedia module is
involved: recording and playback ride on sounddevice (PortAudio), the same
optional dependency the call engine prefers. Everything degrades gracefully
when sounddevice is missing — the UI hides the record button and the voice
bubble opens the file externally instead of playing inline.
"""

import os
import threading
import time
import wave

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPWIDTH = 2

try:  # optional dependency (windows/README.md): absent -> features degrade
    import numpy as _np  # sounddevice callbacks deliver numpy buffers
    import sounddevice as _sd
except Exception:  # pragma: no cover - depends on the environment
    _np = None
    _sd = None


def audio_available() -> bool:
    return _sd is not None


def wav_duration_seconds(path: str) -> int:
    """Whole-second duration of a WAV file (0 when unreadable). Reads only
    the header, so it is safe to call on the UI thread. Half-up rounding,
    exactly like the Android client's `Math.round` — the two ends must label
    the same clip with the same duration."""
    try:
        with wave.open(path, "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate() or SAMPLE_RATE
    except Exception:
        return 0
    if rate <= 0:
        return 0
    return max(0, int(frames / rate + 0.5))


def format_voice_duration(seconds: int) -> str:
    """0:07 style label."""
    return f"{seconds // 60}:{seconds % 60:02d}"


def prune_recordings(recordings_dir: str, max_age_seconds: int = 3600) -> None:
    """Delete recording sources older than [max_age_seconds]. Called once at
    startup: the previous session's file offers are already dead (their
    download addresses are blanked on restore), so their WAVs can never be
    served again — without this, every sent voice message left a copy behind
    forever. The media-dir mirror is kept: it is what the sender's own bubble
    plays back."""
    cutoff = time.time() - max(0, int(max_age_seconds))
    try:
        names = os.listdir(recordings_dir)
    except OSError:
        return
    for name in names:
        if not name.endswith(".wav"):
            continue
        path = os.path.join(recordings_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            continue


class VoiceRecorder:
    """Capture the default microphone into a 16 kHz mono WAV. start() returns
    False when capture cannot begin (no sounddevice / no input device)."""

    def __init__(self, recordings_dir: str):
        self._dir = recordings_dir
        self._path: str = ""
        self._stream = None
        self._file = None
        self._started_at = 0.0
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def _output_path(self) -> str:
        os.makedirs(self._dir, exist_ok=True)
        return os.path.join(self._dir, f"voice_{int(time.time() * 1000)}.wav")

    def start(self) -> bool:
        if _sd is None or self.recording:
            return False
        path = self._output_path()
        f = None
        stream = None
        try:
            f = wave.open(path, "wb")
            f.setnchannels(CHANNELS)
            f.setsampwidth(SAMPWIDTH)
            f.setframerate(SAMPLE_RATE)

            def callback(indata, frames, time_info, status) -> None:
                # snapshot once: stop() may clear the attribute concurrently
                target = self._file
                if target is not None:
                    try:
                        target.writeframes(indata)
                    except Exception:
                        pass

            stream = _sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=SAMPLE_RATE // 10,
                callback=callback,
            )
            # publish the state BEFORE the stream starts, so the first
            # callback already sees the open file (no dropped head frames)
            with self._lock:
                self._path = path
                self._file = f
                self._stream = stream
                self._started_at = time.monotonic()
            stream.start()
        except Exception:
            with self._lock:
                self._path = ""
                self._file = None
                self._stream = None
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
            return False
        return True

    def elapsed_seconds(self) -> int:
        with self._lock:
            if not self.recording:
                return 0
            return int(time.monotonic() - self._started_at)

    def stop(self) -> str:
        """Finish capture and return the recording path ("" when nothing was
        recorded). A clip counts as usable when it holds at least one sample —
        the same rule as Android (which only rejects zero-length data), so a
        very short tap is kept and labelled "0:00" on both ends instead of
        silently vanishing on one of them."""
        with self._lock:
            stream, f, path = self._stream, self._file, self._path
            self._stream = None
            self._file = None
            self._path = ""
        if stream is None:
            return ""
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        try:
            f.close()
        except Exception:
            pass
        try:
            with wave.open(path, "rb") as w:
                frames = w.getnframes()
        except Exception:
            frames = 0
        if frames <= 0:
            try:
                os.remove(path)
            except OSError:
                pass
            return ""
        return path

    def cancel(self) -> None:
        """Abort capture and discard the recording (navigation away, page
        teardown). Unlike stop() the file is removed: the clip was never
        offered to anyone, so it must not linger as an orphan WAV."""
        path = self.stop()
        if not path:
            return
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


class VoicePlayer:
    """Play one WAV file through the default output. A new play() replaces the
    current one; stop() halts playback. [on_finished] fires once per finished
    (or replaced) playback, from the monitor thread — the caller must hop to
    the UI thread itself when it touches widgets."""

    def __init__(self, on_finished=None):
        self._stream = None
        self._wave = None
        self._lock = threading.Lock()
        self._on_finished = on_finished
        self._generation = 0

    def play(self, path: str) -> bool:
        if _sd is None:
            return False
        self.stop()
        try:
            w = wave.open(path, "rb")
            if (
                w.getframerate() != SAMPLE_RATE
                or w.getnchannels() != CHANNELS
                or w.getsampwidth() != SAMPWIDTH
            ):
                w.close()
                return False
        except Exception:
            return False

        source = {"pos": 0}

        def callback(outdata, frames, time_info, status) -> None:
            with self._lock:
                f = self._wave if self._stream is not None else None
            if f is None:
                outdata.fill(0)
                raise _sd.CallbackStop
            data = f.readframes(frames)
            done = len(data) // (SAMPWIDTH * CHANNELS)
            if done <= 0:
                outdata.fill(0)
                raise _sd.CallbackStop
            # outdata is a numpy int16 buffer shaped (frames, CHANNELS): raw
            # bytes must be decoded via frombuffer and reshaped (same as
            # call.py), not assigned directly.
            outdata[:done] = _np.frombuffer(
                data, dtype=_np.int16, count=done * CHANNELS
            ).reshape(-1, CHANNELS)
            if done < frames:
                outdata[done:] = 0
                raise _sd.CallbackStop

        stream = None
        try:
            stream = _sd.OutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=SAMPLE_RATE // 10,
                callback=callback,
            )
            stream.start()
        except Exception:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            try:
                w.close()
            except Exception:
                pass
            return False
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._wave = w
            self._stream = stream

        def monitor() -> None:
            # the callback raises CallbackStop at the natural end; this
            # watcher notices the inactive stream and frees everything
            while True:
                time.sleep(0.05)
                with self._lock:
                    if generation != self._generation:
                        return
                try:
                    if not stream.active:
                        break
                except Exception:
                    break
            finished = True
            with self._lock:
                if generation != self._generation:
                    finished = False  # replaced by a newer play(), not ended
            self._cleanup(generation)
            if finished:
                cb = self._on_finished
                if cb is not None:
                    try:
                        cb()
                    except Exception:
                        pass

        threading.Thread(target=monitor, daemon=True).start()
        return True

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            generation = self._generation
        self._cleanup(generation)

    def _cleanup(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            stream, w = self._stream, self._wave
            self._stream = None
            self._wave = None
        if stream is None:
            return
        for closer in (
            lambda: stream.stop(),
            lambda: stream.close(),
            lambda: w.close(),
        ):
            try:
                closer()
            except Exception:
                pass
