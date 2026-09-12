#!/usr/bin/env python3
"""Offline regression suite for fire_alarm_listener.py.

Uses two real FSKJ222 recordings supplied by the user plus synthetic negatives.
No microphone or Homebridge connection is required.
"""

from __future__ import annotations

import argparse
import math
import wave
from pathlib import Path

import numpy as np

from fire_alarm_listener import FireAlarmDetector, FRAME_SAMPLES, HOP_SAMPLES, SAMPLE_RATE


def read_wav_mono_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != SAMPLE_RATE:
            raise ValueError(f"{path}: expected {SAMPLE_RATE} Hz, got {wf.getframerate()} Hz")
        if wf.getnchannels() != 1:
            raise ValueError(f"{path}: expected mono WAV")
        if wf.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit PCM WAV")
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def run_detector(audio: np.ndarray) -> tuple[list[tuple[float, dict]], list[tuple[float, dict]]]:
    detector = FireAlarmDetector()
    events: list[tuple[float, dict]] = []
    sweeps: list[tuple[float, dict]] = []
    for start in range(0, len(audio) - FRAME_SAMPLES + 1, HOP_SAMPLES):
        t = (start + HOP_SAMPLES) / SAMPLE_RATE
        event, metrics = detector.update(audio[start : start + FRAME_SAMPLES], t)
        if metrics.get("sweep") is not None:
            sweeps.append((t, metrics["sweep"]))
        if event:
            events.append((t, metrics))
    return events, sweeps


def clip(audio: np.ndarray, start_s: float, end_s: float) -> np.ndarray:
    return audio[int(start_s * SAMPLE_RATE) : int(end_s * SAMPLE_RATE)]


def attenuate(audio: np.ndarray, db: float) -> np.ndarray:
    return audio * (10.0 ** (-db / 20.0))


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(round(seconds * SAMPLE_RATE)), dtype=np.float32)


def tone(freq_hz: float, seconds: float, amp: float = 0.25) -> np.ndarray:
    t = np.arange(int(round(seconds * SAMPLE_RATE)), dtype=np.float64) / SAMPLE_RATE
    return (amp * np.sin(2.0 * np.pi * freq_hz * t)).astype(np.float32)


def chirp(f0_hz: float, f1_hz: float, seconds: float, amp: float = 0.25) -> np.ndarray:
    t = np.arange(int(round(seconds * SAMPLE_RATE)), dtype=np.float64) / SAMPLE_RATE
    k = (f1_hz - f0_hz) / seconds
    phase = 2.0 * np.pi * (f0_hz * t + 0.5 * k * t * t)
    return (amp * np.sin(phase)).astype(np.float32)


def white_noise(seconds: float, amp: float = 0.08, seed: int = 222) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal(int(round(seconds * SAMPLE_RATE)))).astype(np.float32)


def speech_like(seconds: float, amp: float = 0.12) -> np.ndarray:
    """Crude voiced/harmonic negative fixture; intentionally not real speech."""
    t = np.arange(int(round(seconds * SAMPLE_RATE)), dtype=np.float64) / SAMPLE_RATE
    f0 = 145.0 + 25.0 * np.sin(2 * np.pi * 1.7 * t)
    phase = 2 * np.pi * np.cumsum(f0) / SAMPLE_RATE
    sig = np.zeros_like(t)
    for h in range(1, 12):
        sig += (1.0 / h) * np.sin(h * phase)
    env = 0.45 + 0.55 * np.sin(2 * np.pi * 3.2 * t) ** 2
    sig = sig / (np.max(np.abs(sig)) + 1e-12)
    return (amp * env * sig).astype(np.float32)


def synthetic_cases() -> list[tuple[str, np.ndarray, int]]:
    exact_cycle = np.concatenate(
        [
            tone(2703, 0.95),
            silence(0.07),
            chirp(520, 3820, 0.90),
            silence(0.35),
            chirp(520, 3820, 0.90),
            silence(0.5),
        ]
    )
    wrong_pair_gap = np.concatenate(
        [
            tone(2703, 0.95),
            silence(0.07),
            chirp(520, 3820, 0.90),
            silence(0.80),
            chirp(520, 3820, 0.90),
        ]
    )
    wrong_sweep = np.concatenate(
        [
            tone(2703, 0.95),
            silence(0.07),
            chirp(520, 2500, 0.90),
            silence(0.35),
            chirp(520, 2500, 0.90),
        ]
    )
    fixed_siren = np.concatenate(
        [tone(2703, 0.95), silence(0.08), tone(1800, 0.85), silence(0.35), tone(1800, 0.85)]
    )
    return [
        ("synthetic/exact_structure", exact_cycle, 1),
        ("negative/2.7kHz_tone_only", tone(2703, 4.0), 0),
        ("negative/white_noise", white_noise(8.0), 0),
        ("negative/speech_like", speech_like(8.0), 0),
        ("negative/fixed_siren", fixed_siren, 0),
        ("negative/wrong_pair_gap", wrong_pair_gap, 0),
        ("negative/wrong_sweep_span", wrong_sweep, 0),
        ("negative/two_sweeps_no_preamble", np.concatenate([chirp(520, 3820, 0.9), silence(0.35), chirp(520, 3820, 0.9)]), 0),
        ("negative/preamble_plus_one_sweep", np.concatenate([tone(2703, 0.95), silence(0.07), chirp(520, 3820, 0.9)]), 0),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--room", default="fskj222_room_16k.wav")
    parser.add_argument("--direct", default="fskj222_direct_16k.wav")
    args = parser.parse_args()

    room = read_wav_mono_16k(Path(args.room))
    direct = read_wav_mono_16k(Path(args.direct))

    # Boundaries are regression fixtures only; production detection does not
    # hard-code these timestamps.
    tests: list[tuple[str, np.ndarray, int]] = [
        ("real/room_full", room, 1),
        ("real/room_preamble_only", clip(room, 3.45, 4.65), 0),
        ("real/room_preamble_plus_one_sweep", clip(room, 3.45, 5.85), 0),
        ("real/room_two_sweeps_no_preamble", clip(room, 4.65, 7.10), 0),
        ("real/room_voice_only", clip(room, 7.10, 9.80), 0),
        ("real/room_minus_6dB", attenuate(room, 6), 1),
        ("real/room_minus_12dB", attenuate(room, 12), 1),
        ("real/room_minus_18dB", attenuate(room, 18), 1),
        ("real/direct_full_three_cycles", direct, 3),
        ("real/direct_preamble_only", clip(direct, 0.0, 1.10), 0),
        ("real/direct_one_sweep_only", clip(direct, 0.0, 2.05), 0),
        ("real/direct_two_sweeps_no_preamble", clip(direct, 1.05, 3.25), 0),
        ("real/direct_voice_only_1", clip(direct, 3.25, 5.0), 0),
        ("real/direct_cycle_2", clip(direct, 5.0, 8.4), 1),
        ("real/direct_voice_only_2", clip(direct, 8.3, 10.1), 0),
        ("real/direct_minus_18dB", attenuate(direct, 18), 3),
        ("real/direct_minus_30dB", attenuate(direct, 30), 3),
    ]
    tests.extend(synthetic_cases())

    failed = 0
    print("FSKJ222 detector regression\n")
    for name, data, expected in tests:
        events, sweeps = run_detector(data)
        got = len(events)
        ok = got == expected
        print(
            f"{'PASS' if ok else 'FAIL'}  {name:38s} "
            f"expected_events={expected:<2d} got={got:<2d} valid_sweeps={len(sweeps)}"
        )
        if events:
            t, metrics = events[0]
            sw = metrics.get("sweep") or {}
            print(
                "      first_event="
                f"{t:.3f}s slope={sw.get('slope_hz_s')}Hz/s "
                f"r2={sw.get('r2')} span={sw.get('span_hz')}Hz "
                f"prom={sw.get('median_prominence_db')}dB"
            )
        failed += 0 if ok else 1

    if failed:
        print(f"\n{failed} regression test(s) FAILED")
        return 1
    print(f"\nAll {len(tests)} regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
