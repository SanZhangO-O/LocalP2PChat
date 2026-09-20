"""Group conference mixing rule (group_call.mix_pcm16).

Android parity: GroupCallMixTest pins the same vectors on the Kotlin side
(ConferenceMix.mixPcm). A mismatch between the two would garble every mixed
conference frame cross-platform.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from localchat.group_call import AUDIO_FRAME_BYTES, mix_pcm16


def frame(*samples, frame_bytes=4):
    """One PCM16 little-endian mono buffer holding [samples]; grows past
    frame_bytes when more samples are given (the oversized-chunk test)."""
    out = bytearray(max(frame_bytes, len(samples) * 2))
    for i, v in enumerate(samples):
        out[i * 2] = v & 0xFF
        out[i * 2 + 1] = (v >> 8) & 0xFF
    return bytes(out)


def sample_at(pcm, index):
    return int.from_bytes(pcm[index * 2:index * 2 + 2], "little", signed=True)


class GroupCallMixTest(unittest.TestCase):
    def test_empty_mix_is_silence(self):
        mixed = mix_pcm16([])
        self.assertEqual(AUDIO_FRAME_BYTES, len(mixed))
        for i in range(AUDIO_FRAME_BYTES // 2):
            self.assertEqual(0, sample_at(mixed, i))

    def test_two_voices_sum(self):
        mixed = mix_pcm16([frame(1000), frame(2000)], 4)
        self.assertEqual(3000, sample_at(mixed, 0))

    def test_clipping_saturates_instead_of_wrapping(self):
        hi = mix_pcm16([frame(20000), frame(20000)], 4)
        self.assertEqual(32767, sample_at(hi, 0))
        lo = mix_pcm16([frame(-20000), frame(-20000)], 4)
        self.assertEqual(-32768, sample_at(lo, 0))

    def test_a_short_chunk_is_zero_padded_not_dropped(self):
        mixed = mix_pcm16([frame(500), b""], 4)
        self.assertEqual(500, sample_at(mixed, 0))

    def test_an_oversized_chunk_is_truncated_to_one_frame(self):
        mixed = mix_pcm16([frame(100, 100, 100, 100)], 4)
        self.assertEqual(100, sample_at(mixed, 0))
        self.assertEqual(100, sample_at(mixed, 1))

    def test_frame_size_matches_the_20ms_contract(self):
        self.assertEqual(640, AUDIO_FRAME_BYTES)


if __name__ == "__main__":
    unittest.main()
