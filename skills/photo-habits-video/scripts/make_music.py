#!/usr/bin/env python3
"""Synthesize an original, gentle instrumental cue locally (NumPy + stdlib).

No recordings, model calls, downloaded music, or third-party melodies are used.
"""
import argparse
import json
import math
from pathlib import Path
import wave

import numpy as np


def generate_music(output, duration=60.0, bpm=102, sample_rate=48000, seed=27):
    if not 0 < duration <= 600:
        raise ValueError("duration must be between 0 and 600 seconds")
    if not 40 <= bpm <= 200:
        raise ValueError("bpm must be between 40 and 200")
    rng = np.random.default_rng(seed)
    n = max(1, round(sample_rate * duration))
    audio = np.zeros((n, 2), dtype=np.float32)
    beat = 60 / bpm

    def add(signal, at, gain, pan=0):
        start = round(at * sample_rate)
        if start >= n:
            return
        if start < 0:
            signal = signal[-start:]
            start = 0
        count = min(len(signal), n - start)
        angle = (pan + 1) * math.pi / 4
        audio[start:start + count, 0] += signal[:count] * gain * math.cos(angle)
        audio[start:start + count, 1] += signal[:count] * gain * math.sin(angle)

    def tone(note, length, soft=True):
        t = np.arange(round(length * sample_rate), dtype=np.float32) / sample_rate
        hz = 440 * 2 ** ((note - 69) / 12)
        attack = .012 if soft else .007
        decay = .65 if soft else .9
        env = (1 - np.exp(-t / attack)) * np.exp(-t / decay)
        env *= np.minimum(1, np.maximum(0, length - t) / .12)
        return (np.sin(2 * np.pi * hz * t) + .13 * np.sin(4 * np.pi * hz * t)) * env

    t = np.arange(round(.36 * sample_rate)) / sample_rate
    kick = np.sin(2 * np.pi * np.cumsum(48 + 55 * np.exp(-t / .025)) / sample_rate)
    kick *= (1 - np.exp(-t / .002)) * np.exp(-t / .08)
    t = np.arange(round(.08 * sample_rate)) / sample_rate
    hat = rng.normal(size=len(t)).astype(np.float32)
    hat = np.diff(hat, prepend=hat[0])
    hat /= max(1.0, np.max(np.abs(hat)))
    hat *= np.exp(-t / .019) * (1 - np.exp(-t / .001))
    # Simple harmonic accompaniment, generated note by note rather than sampled.
    chords = [([57, 60, 64, 71], 45), ([53, 57, 60, 67], 41),
              ([55, 60, 64, 71], 36), ([55, 59, 62, 69], 43)]
    for bar in range(math.ceil(duration / (4 * beat))):
        chord, root = chords[(bar // 2) % len(chords)]
        start = bar * 4 * beat
        for position, gain in [(0, .045), (2, .028)]:
            for j, note in enumerate(chord):
                add(tone(note, 2.2), start + position * beat + j * .012,
                    gain, [-.45, -.15, .15, .45][j])
        for position, note, gain in [(0, root, .09), (1.75, root, .05),
                                     (2.5, root + 7, .06), (3.5, root, .04)]:
            add(tone(note, 1.1, False), start + position * beat, gain)
        for position in range(4):
            add(kick, start + position * beat, .12 if position % 2 == 0 else .035)
            add(hat, start + (position + .5) * beat, .035, .22)
        if bar:
            add(tone(chord[2] + 12, 1.1), start + 1.5 * beat, .015, -.25)
            add(tone(chord[3] + 12, 1.1), start + 3.25 * beat, .015, .25)
    dry = audio.copy()
    for seconds, gain in [(.137, .10), (.283, .06), (.421, .03)]:
        lag = round(seconds * sample_rate)
        if lag < n:
            audio[lag:] += dry[:-lag, ::-1] * gain
    audio -= audio.mean(axis=0)
    audio = np.tanh(audio * 1.3) / 1.3
    t = np.arange(n) / sample_rate
    fade_in = min(.8, duration / 4)
    fade_out = min(1.5, duration / 3)
    fade = np.minimum(1, t / fade_in) * np.minimum(1, np.maximum(0, duration - t) / fade_out)
    audio *= np.sin(fade * np.pi / 2)[:, None] ** 1.5
    peak = float(np.max(np.abs(audio)))
    if peak:
        audio *= .68 / peak
    pcm = np.rint(audio * 32767).astype('<i2')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), 'wb') as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())
    stats = {'duration_seconds': n / sample_rate, 'sample_rate': sample_rate,
             'channels': 2, 'bpm': bpm, 'seed': seed, 'original_synthesis': True,
             'peak_dbfs': round(20 * math.log10(max(float(np.max(np.abs(audio))), 1e-12)), 2),
             'clipped_samples': int(np.count_nonzero(np.abs(pcm.astype(np.int32)) >= 32767))}
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--duration', type=float, default=60)
    parser.add_argument('--bpm', type=float, default=102)
    parser.add_argument('--seed', type=int, default=27)
    args = parser.parse_args()
    try:
        stats = generate_music(args.output, args.duration, args.bpm, seed=args.seed)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Music generation failed: {exc}\n')
    args.output.with_suffix('.music.json').write_text(json.dumps(stats, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), **stats}))


if __name__ == '__main__':
    main()
