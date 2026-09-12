#!/usr/bin/env python3
"""
Standalone macOS acoustic detector for NOHMI FSKJ222-style smoke alarms.

Fingerprint used by this detector (measured from the two user-provided samples):
  1) a sustained ~2.70 kHz preamble tone,
  2) two rising tonal sweeps, each ~0.8 s long,
  3) the two sweep starts separated by ~1.26 s.

The Japanese speech that follows the alarm tones is intentionally ignored.
This makes the detector language-independent and less likely to react to TV
speech or ordinary household audio.

This is a SECONDARY notification bridge only. It does not replace a certified
smoke detector, fire alarm, or emergency notification system.

Runtime dependencies:
    numpy
    sounddevice

Default Homebridge target:
    homebridge-http-webhooks on http://127.0.0.1:51828/
    accessoryId=livingRoomFireAlarm
    state=true / state=false
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

import numpy as np

sd = None  # lazy import: offline regression only needs NumPy

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Audio / feature extraction
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1024          # 64 ms FFT window
HOP_SAMPLES = 256             # 16 ms feature cadence
BLOCK_SAMPLES = 512           # 32 ms PortAudio callback

BAND_LOW_HZ = 300.0
BAND_HIGH_HZ = 4300.0

# Preamble measured in both samples at ~2703 Hz. Narrow bounds are deliberate:
# they are one of the main protections against random TV / speech false alarms.
PREAMBLE_LOW_HZ = 2620.0
PREAMBLE_HIGH_HZ = 2780.0
PREAMBLE_MIN_SECONDS = 0.35
PREAMBLE_MAX_AGE_SECONDS = 0.45
PREAMBLE_MIN_PROMINENCE_DB = 24.0
PREAMBLE_MIN_CONCENTRATION = 0.80

# Sweep envelope. The room sample is slightly noisier than the direct sample,
# so these limits leave useful margin without accepting generic sirens.
SWEEP_START_LOW_HZ = 500.0
SWEEP_START_HIGH_HZ = 1300.0
SWEEP_END_MIN_HZ = 3300.0
SWEEP_POINT_LOW_HZ = 350.0
SWEEP_POINT_HIGH_HZ = 4300.0
SWEEP_MIN_SECONDS = 0.68
SWEEP_MAX_SECONDS = 0.98
SWEEP_GAP_TOLERANCE_SECONDS = 0.12
SWEEP_MIN_SLOPE_HZ_PER_S = 3000.0
SWEEP_MAX_SLOPE_HZ_PER_S = 4100.0
SWEEP_MIN_R2 = 0.95
SWEEP_MIN_SPAN_HZ = 2200.0
SWEEP_MIN_MONOTONIC_FRACTION = 0.90

# Observed sweep-start spacing is ~1.26 s in both samples.
PAIR_MIN_INTERVAL_SECONDS = 1.10
PAIR_MAX_INTERVAL_SECONDS = 1.45

# Frame / sweep quality gates. These reject broad-band speech, music and noise.
MIN_FRAME_DBFS = -60.0
MIN_PEAK_PROMINENCE_DB = 15.0
MIN_PEAK_CONCENTRATION = 0.50
MIN_SWEEP_MEDIAN_PROMINENCE_DB = 22.0
MIN_SWEEP_MEDIAN_CONCENTRATION = 0.75
MIN_SWEEP_MEDIAN_DBFS = -58.0

# HomeKit state policy.
DEFAULT_CLEAR_AFTER_SECONDS = 30.0
DEFAULT_STARTUP_CLEAR_AFTER_SECONDS = 20.0
DEFAULT_WEBHOOK_BASE = "http://127.0.0.1:51828/"
DEFAULT_ACCESSORY_ID = "livingRoomFireAlarm"

# Optional extra-conservative policy: require two complete alarm cycles before
# raising HomeKit. Direct reference cycles are ~5.05 s apart.
CONFIRM_CYCLE_MIN_GAP_SECONDS = 3.5
CONFIRM_CYCLE_MAX_GAP_SECONDS = 6.5


@dataclass
class Feature:
    t: float
    freq_hz: float
    prominence_db: float
    concentration: float
    dbfs: float


@dataclass
class SweepResult:
    start_t: float
    end_t: float
    duration_s: float
    slope_hz_s: float
    r2: float
    span_hz: float
    monotonic_fraction: float
    first_hz: float
    last_hz: float
    median_prominence_db: float
    median_concentration: float
    median_dbfs: float


class FireAlarmDetector:
    """Streaming state machine for the FSKJ222 acoustic fingerprint."""

    def __init__(self) -> None:
        self.window = np.hanning(FRAME_SAMPLES).astype(np.float32)
        self.freqs = np.fft.rfftfreq(FRAME_SAMPLES, d=1.0 / SAMPLE_RATE)
        self.band_mask = (self.freqs >= BAND_LOW_HZ) & (self.freqs <= BAND_HIGH_HZ)
        self.band_freqs = self.freqs[self.band_mask]

        self.tone_run_start: float | None = None
        self.last_tone_time: float | None = None
        self.last_preamble_time = -1e9

        self.sweep_points: list[Feature] = []
        self.sweep_start: float | None = None
        self.last_sweep_point_time: float | None = None
        self.sweep_starts: collections.deque[float] = collections.deque(maxlen=4)
        self.cooldown_until = -1e9

        self.valid_sweep_count = 0
        self.complete_cycle_count = 0

    def analyze_frame(self, frame: np.ndarray, t: float) -> Feature:
        frame = np.asarray(frame, dtype=np.float32)
        if len(frame) != FRAME_SAMPLES:
            raise ValueError(f"Expected {FRAME_SAMPLES} samples, got {len(frame)}")

        frame = frame - float(np.mean(frame))
        rms = math.sqrt(float(np.mean(frame * frame)) + 1e-15)
        dbfs = 20.0 * math.log10(max(rms, 1e-12))

        mag = np.abs(np.fft.rfft(frame * self.window))
        band_mag = mag[self.band_mask]

        peak_i = int(np.argmax(band_mag))
        peak_hz = float(self.band_freqs[peak_i])
        peak_mag = float(band_mag[peak_i])

        delta = np.abs(self.band_freqs - peak_hz)
        local_noise = band_mag[(delta >= 90.0) & (delta <= 350.0)]
        noise_mag = float(np.median(local_noise)) + 1e-12 if len(local_noise) else 1e-12
        prominence_db = 20.0 * math.log10((peak_mag + 1e-12) / noise_mag)

        concentrated_power = float(np.sum(band_mag[delta <= 80.0] ** 2))
        band_power = float(np.sum(band_mag ** 2)) + 1e-12
        concentration = concentrated_power / band_power

        return Feature(t, peak_hz, prominence_db, concentration, dbfs)

    @staticmethod
    def _is_tonal(f: Feature) -> bool:
        return (
            f.dbfs >= MIN_FRAME_DBFS
            and f.prominence_db >= MIN_PEAK_PROMINENCE_DB
            and f.concentration >= MIN_PEAK_CONCENTRATION
        )

    def _update_preamble(self, f: Feature) -> None:
        is_preamble = (
            self._is_tonal(f)
            and PREAMBLE_LOW_HZ <= f.freq_hz <= PREAMBLE_HIGH_HZ
            and f.prominence_db >= PREAMBLE_MIN_PROMINENCE_DB
            and f.concentration >= PREAMBLE_MIN_CONCENTRATION
        )

        if is_preamble:
            if self.tone_run_start is None:
                self.tone_run_start = f.t
            self.last_tone_time = f.t
            if f.t - self.tone_run_start >= PREAMBLE_MIN_SECONDS:
                self.last_preamble_time = f.t
        elif self.last_tone_time is not None and f.t - self.last_tone_time > 0.10:
            self.tone_run_start = None
            self.last_tone_time = None

    @staticmethod
    def _evaluate_sweep(points: list[Feature]) -> SweepResult | None:
        if len(points) < 20:
            return None

        ts = np.array([p.t for p in points], dtype=np.float64)
        hz = np.array([p.freq_hz for p in points], dtype=np.float64)
        duration = float(ts[-1] - ts[0])
        if not (SWEEP_MIN_SECONDS <= duration <= SWEEP_MAX_SECONDS):
            return None

        x = ts - ts[0]
        slope, intercept = np.polyfit(x, hz, 1)
        predicted = slope * x + intercept
        ss_res = float(np.sum((hz - predicted) ** 2))
        ss_tot = float(np.sum((hz - np.mean(hz)) ** 2)) + 1e-9
        r2 = 1.0 - ss_res / ss_tot

        span = float(np.percentile(hz, 90) - np.percentile(hz, 10))
        monotonic_fraction = float(np.mean(np.diff(hz) >= -125.0))
        edge_n = max(3, len(hz) // 5)
        first_hz = float(np.median(hz[:edge_n]))
        last_hz = float(np.median(hz[-edge_n:]))
        median_prom = float(np.median([p.prominence_db for p in points]))
        median_conc = float(np.median([p.concentration for p in points]))
        median_dbfs = float(np.median([p.dbfs for p in points]))

        if not (
            SWEEP_MIN_SLOPE_HZ_PER_S <= slope <= SWEEP_MAX_SLOPE_HZ_PER_S
            and r2 >= SWEEP_MIN_R2
            and span >= SWEEP_MIN_SPAN_HZ
            and monotonic_fraction >= SWEEP_MIN_MONOTONIC_FRACTION
            and first_hz <= 1300.0
            and last_hz >= 3000.0
            and median_prom >= MIN_SWEEP_MEDIAN_PROMINENCE_DB
            and median_conc >= MIN_SWEEP_MEDIAN_CONCENTRATION
            and median_dbfs >= MIN_SWEEP_MEDIAN_DBFS
        ):
            return None

        return SweepResult(
            start_t=float(ts[0]),
            end_t=float(ts[-1]),
            duration_s=duration,
            slope_hz_s=float(slope),
            r2=float(r2),
            span_hz=span,
            monotonic_fraction=monotonic_fraction,
            first_hz=first_hz,
            last_hz=last_hz,
            median_prominence_db=median_prom,
            median_concentration=median_conc,
            median_dbfs=median_dbfs,
        )

    def _reset_sweep(self) -> None:
        self.sweep_points = []
        self.sweep_start = None
        self.last_sweep_point_time = None

    def update(self, frame: np.ndarray, t: float) -> tuple[bool, dict]:
        f = self.analyze_frame(frame, t)
        self._update_preamble(f)

        alarm_event = False
        sweep_result: SweepResult | None = None
        tonal = self._is_tonal(f)

        if f.t >= self.cooldown_until:
            if not self.sweep_points:
                if tonal and SWEEP_START_LOW_HZ <= f.freq_hz <= SWEEP_START_HIGH_HZ:
                    self.sweep_points = [f]
                    self.sweep_start = f.t
                    self.last_sweep_point_time = f.t
            else:
                assert self.sweep_start is not None
                assert self.last_sweep_point_time is not None
                age = f.t - self.sweep_start
                gap = f.t - self.last_sweep_point_time

                if age > 1.20 or gap > SWEEP_GAP_TOLERANCE_SECONDS:
                    self._reset_sweep()
                    if tonal and SWEEP_START_LOW_HZ <= f.freq_hz <= SWEEP_START_HIGH_HZ:
                        self.sweep_points = [f]
                        self.sweep_start = f.t
                        self.last_sweep_point_time = f.t
                elif tonal and SWEEP_POINT_LOW_HZ <= f.freq_hz <= SWEEP_POINT_HIGH_HZ:
                    self.sweep_points.append(f)
                    self.last_sweep_point_time = f.t

                    if age >= SWEEP_MIN_SECONDS and f.freq_hz >= SWEEP_END_MIN_HZ:
                        sweep_result = self._evaluate_sweep(self.sweep_points)
                        if sweep_result is not None:
                            self.valid_sweep_count += 1
                            start_t = sweep_result.start_t

                            while self.sweep_starts and start_t - self.sweep_starts[0] > 1.8:
                                self.sweep_starts.popleft()
                            self.sweep_starts.append(start_t)

                            if len(self.sweep_starts) >= 2:
                                first = self.sweep_starts[-2]
                                second = self.sweep_starts[-1]
                                interval = second - first
                                preamble_age = first - self.last_preamble_time
                                if (
                                    0.0 <= preamble_age <= PREAMBLE_MAX_AGE_SECONDS
                                    and PAIR_MIN_INTERVAL_SECONDS <= interval <= PAIR_MAX_INTERVAL_SECONDS
                                ):
                                    alarm_event = True
                                    self.complete_cycle_count += 1

                            self.cooldown_until = f.t + 0.12
                            self._reset_sweep()

        metrics = {
            "dbfs": round(f.dbfs, 1),
            "peak_hz": int(round(f.freq_hz)),
            "prominence_db": round(f.prominence_db, 1),
            "concentration": round(f.concentration, 3),
            "preamble_age_s": None
            if self.last_preamble_time < -1e8
            else round(max(0.0, f.t - self.last_preamble_time), 2),
            "valid_sweeps": self.valid_sweep_count,
            "complete_cycles": self.complete_cycle_count,
            "sweep": None,
        }
        if sweep_result is not None:
            metrics["sweep"] = {
                "duration_s": round(sweep_result.duration_s, 3),
                "slope_hz_s": round(sweep_result.slope_hz_s),
                "r2": round(sweep_result.r2, 4),
                "span_hz": round(sweep_result.span_hz),
                "monotonic": round(sweep_result.monotonic_fraction, 3),
                "first_hz": round(sweep_result.first_hz),
                "last_hz": round(sweep_result.last_hz),
                "median_prominence_db": round(sweep_result.median_prominence_db, 1),
                "median_concentration": round(sweep_result.median_concentration, 3),
                "median_dbfs": round(sweep_result.median_dbfs, 1),
            }

        return alarm_event, metrics


# ---------------------------------------------------------------------------
# Homebridge integration
# ---------------------------------------------------------------------------
def homebridge_set_state(base_url: str, accessory_id: str, state: bool, retries: int = 3) -> None:
    query = urllib.parse.urlencode(
        {"accessoryId": accessory_id, "state": "true" if state else "false"}
    )
    separator = "&" if "?" in base_url else "?"
    url = f"{base_url}{separator}{query}"

    last_exc: Exception | None = None
    delays = (0.0, 0.5, 2.0)
    for attempt in range(max(1, retries)):
        if attempt < len(delays) and delays[attempt] > 0:
            time.sleep(delays[attempt])
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as response:
                body = response.read(4096).decode("utf-8", errors="replace")
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}: {body}")
                # The plugin normally returns {"success": true}. If it returns
                # JSON with success=false, treat that as a failed update.
                try:
                    parsed = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    parsed = {}
                if parsed.get("success") is False:
                    raise RuntimeError(f"Homebridge rejected update: {body}")
            return
        except Exception as exc:  # network boundary: retry intentionally broad
            last_exc = exc
    raise RuntimeError(f"Homebridge webhook failed after {retries} attempts: {last_exc}")


class HomebridgeStatePublisher:
    """Serializes state changes and keeps retrying the latest desired state."""

    def __init__(self, *, enabled: bool, base_url: str, accessory_id: str) -> None:
        self.enabled = enabled
        self.base_url = base_url
        self.accessory_id = accessory_id
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._desired_state: bool | None = None
        self._reason = ""
        self._last_sent_state: bool | None = None
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def set(self, state: bool, reason: str) -> None:
        with self._lock:
            self._desired_state = state
            self._reason = reason
        self._wake.set()

    def _worker(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()

            while True:
                with self._lock:
                    desired = self._desired_state
                    reason = self._reason
                    already_sent = desired is not None and desired == self._last_sent_state

                if desired is None or already_sent:
                    break

                timestamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
                if not self.enabled:
                    print(
                        json.dumps(
                            {
                                "timestamp": timestamp,
                                "event": "homebridge_skipped",
                                "state": desired,
                                "reason": reason,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    with self._lock:
                        self._last_sent_state = desired
                    break

                try:
                    homebridge_set_state(self.base_url, self.accessory_id, desired)
                    print(
                        json.dumps(
                            {
                                "timestamp": timestamp,
                                "event": "homebridge_state",
                                "state": desired,
                                "reason": reason,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    with self._lock:
                        self._last_sent_state = desired
                except Exception as exc:  # keep retrying latest desired state
                    print(f"Homebridge error: {exc}; retrying in 5s", file=sys.stderr, flush=True)
                    # Wake early if desired state changes while Homebridge is down.
                    self._wake.wait(timeout=5.0)
                    self._wake.clear()
                    continue


def macos_notification(title: str, message: str) -> None:
    script = 'on run argv\n display notification item 2 of argv with title item 1 of argv\nend run'
    subprocess.Popen(
        ["osascript", "-e", script, title, message],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def resolve_device(device_arg: str | None):
    if device_arg is None:
        return None
    try:
        return int(device_arg)
    except ValueError:
        return device_arg


def main() -> int:
    global sd
    try:
        import sounddevice as _sd
    except ImportError:
        print(
            "Missing dependency: sounddevice\n"
            "Install with: python3 -m pip install numpy sounddevice",
            file=sys.stderr,
        )
        return 2
    sd = _sd

    parser = argparse.ArgumentParser(
        description="Detect NOHMI FSKJ222-style fire alarm audio and update a Homebridge smoke sensor."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--device", help="PortAudio input device index/name; default is macOS input.")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit.")
    parser.add_argument("--debug", action="store_true", help="Print live detector metrics every 0.5 s.")
    parser.add_argument("--no-homebridge", action="store_true", help="Detect only; do not call Homebridge.")
    parser.add_argument("--no-notification", action="store_true", help="Disable local macOS notifications.")
    parser.add_argument("--webhook-base", default=DEFAULT_WEBHOOK_BASE)
    parser.add_argument("--accessory-id", default=DEFAULT_ACCESSORY_ID)
    parser.add_argument("--clear-after", type=float, default=DEFAULT_CLEAR_AFTER_SECONDS)
    parser.add_argument(
        "--startup-clear-after",
        type=float,
        default=DEFAULT_STARTUP_CLEAR_AFTER_SECONDS,
        help="After quiet startup, force one false state to clear stale HomeKit state.",
    )
    parser.add_argument(
        "--confirm-cycles",
        type=int,
        choices=(1, 2),
        default=1,
        help=(
            "Complete alarm cycles required before setting smoke=true. "
            "1 is recommended; 2 is extra-conservative for TV-heavy rooms (~5 s extra latency)."
        ),
    )
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return 0
    if args.clear_after < 10:
        parser.error("--clear-after must be >= 10 seconds")
    if args.startup_clear_after < 10:
        parser.error("--startup-clear-after must be >= 10 seconds")

    detector = FireAlarmDetector()
    publisher = HomebridgeStatePublisher(
        enabled=not args.no_homebridge,
        base_url=args.webhook_base,
        accessory_id=args.accessory_id,
    )

    audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
    rolling = np.zeros(0, dtype=np.float32)
    device = resolve_device(args.device)

    alarm_active = False
    process_started = time.monotonic()
    last_alarm_evidence = -1e9
    startup_clear_sent = False
    cycle_candidates: collections.deque[float] = collections.deque(maxlen=2)

    def callback(indata, frames, time_info, status):
        if status and args.debug:
            print(f"audio status: {status}", file=sys.stderr, flush=True)
        mono = np.asarray(indata[:, 0], dtype=np.float32).copy()
        try:
            audio_queue.put_nowait(mono)
        except queue.Full:
            # Real-time safety: drop oldest queued audio rather than accumulate lag.
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                audio_queue.put_nowait(mono)
            except queue.Full:
                pass

    print(f"FSKJ222 fire-alarm detector v{VERSION} starting", flush=True)
    print(
        "Fingerprint: ~2.70 kHz preamble + two ~0.8 s rising sweeps; speech ignored.",
        flush=True,
    )
    print(
        f"Activation: {args.confirm_cycles} complete cycle(s); clear after "
        f"{args.clear_after:.0f}s without a complete cycle.",
        flush=True,
    )
    print(
        f"Homebridge: {args.webhook_base} accessoryId={args.accessory_id} "
        f"(disabled={args.no_homebridge})",
        flush=True,
    )

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SAMPLES,
            device=device,
            channels=1,
            dtype="float32",
            callback=callback,
        ):
            frame_clock = 0
            last_debug = 0.0

            while True:
                chunk = audio_queue.get()
                rolling = np.concatenate((rolling, chunk))

                while len(rolling) >= FRAME_SAMPLES:
                    frame = rolling[:FRAME_SAMPLES]
                    rolling = rolling[HOP_SAMPLES:]
                    frame_clock += HOP_SAMPLES
                    t = frame_clock / SAMPLE_RATE

                    complete_cycle, metrics = detector.update(frame, t)
                    now = time.monotonic()

                    if metrics.get("sweep") is not None and args.debug:
                        print(
                            json.dumps(
                                {"event": "valid_sweep", "metrics": metrics["sweep"]},
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )

                    if complete_cycle:
                        last_alarm_evidence = now
                        cycle_candidates.append(now)
                        print(
                            json.dumps(
                                {
                                    "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                                    "event": "complete_alarm_cycle",
                                    "count": detector.complete_cycle_count,
                                    "sweep": metrics.get("sweep"),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )

                        confirmed = args.confirm_cycles == 1
                        if args.confirm_cycles == 2 and len(cycle_candidates) == 2:
                            gap = cycle_candidates[-1] - cycle_candidates[-2]
                            confirmed = CONFIRM_CYCLE_MIN_GAP_SECONDS <= gap <= CONFIRM_CYCLE_MAX_GAP_SECONDS

                        if confirmed and not alarm_active:
                            alarm_active = True
                            publisher.set(True, f"confirmed-{args.confirm_cycles}-cycle")
                            if not args.no_notification:
                                try:
                                    macos_notification(
                                        "Fire alarm detected",
                                        "FSKJ222 acoustic alarm fingerprint detected",
                                    )
                                except Exception as exc:
                                    print(f"Notification error: {exc}", file=sys.stderr, flush=True)

                    if alarm_active and now - last_alarm_evidence >= args.clear_after:
                        alarm_active = False
                        cycle_candidates.clear()
                        publisher.set(False, "quiet-timeout")

                    if (
                        not startup_clear_sent
                        and not alarm_active
                        and now - process_started >= args.startup_clear_after
                    ):
                        startup_clear_sent = True
                        publisher.set(False, "startup-clear")

                    if args.debug and now - last_debug >= 0.5:
                        print(json.dumps(metrics, ensure_ascii=False), flush=True)
                        last_debug = now

    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        return 0
    except Exception as exc:
        print(f"Fatal audio error: {exc}", file=sys.stderr, flush=True)
        print("Run with --list-devices and verify macOS microphone permission.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
