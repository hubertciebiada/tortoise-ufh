"""Original soundtrack for the Tortoise-UFH /brag video, synthesized with numpy.

120 BPM in D major. Chords change on the scene cuts (3, 7, 11, 15, 18 s):
intro pad + plucks + water shimmer, a riser into the downbeat at 3.0 s, a warm
four-on-the-floor groove, a drum-less breakdown under the fable, and a final D
with a soft bell on the end card. UI clicks, whooshes and pops are tuned to the
key and share the music's reverb, mixed under it.

Writes soundtrack.wav (48 kHz, stereo, float32).
"""

from __future__ import annotations

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, fftconvolve, sosfilt

SR = 48_000
DUR = 21.0
N = int(SR * DUR)
BEAT = 0.5  # 120 BPM
rng = np.random.default_rng(7)


def hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def t_axis(n: int) -> np.ndarray:
    return np.arange(n) / SR


def lp(x: np.ndarray, fc: float, order: int = 2) -> np.ndarray:
    return sosfilt(butter(order, fc, "low", fs=SR, output="sos"), x, axis=-1)


def hp(x: np.ndarray, fc: float, order: int = 2) -> np.ndarray:
    return sosfilt(butter(order, fc, "high", fs=SR, output="sos"), x, axis=-1)


def bp(x: np.ndarray, lo: float, hi: float, order: int = 2) -> np.ndarray:
    return sosfilt(butter(order, [lo, hi], "band", fs=SR, output="sos"), x, axis=-1)


class Bus:
    """A stereo bus with a reverb send."""

    def __init__(self) -> None:
        self.dry = np.zeros((2, N))
        self.send = np.zeros((2, N))

    def add(
        self,
        sig: np.ndarray,
        at: float,
        gain: float = 1.0,
        pan: float = 0.0,
        rev: float = 0.0,
    ) -> None:
        i = int(round(at * SR))
        if i >= N:
            return
        sig = sig[: N - i]
        left = np.cos((pan + 1) * np.pi / 4)
        right = np.sin((pan + 1) * np.pi / 4)
        self.dry[0, i : i + len(sig)] += sig * gain * left
        self.dry[1, i : i + len(sig)] += sig * gain * right
        if rev:
            self.send[0, i : i + len(sig)] += sig * gain * rev * left
            self.send[1, i : i + len(sig)] += sig * gain * rev * right


# --- Instruments -----------------------------------------------------------------


def pluck(midi: float, dur: float = 0.55, bright: float = 1.0) -> np.ndarray:
    n = int(dur * SR)
    t = t_axis(n)
    f = hz(midi)
    out = np.zeros(n)
    for k in range(1, 7):
        out += (
            (1 / k**1.35)
            * np.sin(2 * np.pi * f * k * t)
            * np.exp(-t * (4.5 + 2.8 * k / bright))
        )
    out += 0.08 * lp(rng.standard_normal(n), 4000) * np.exp(-t * 90)
    return out * np.minimum(1, t * SR / 40) * 0.5


def bell(midi: float, dur: float = 3.0) -> np.ndarray:
    n = int(dur * SR)
    t = t_axis(n)
    f = hz(midi)
    partials = [
        (1.0, 1.0, 1.4),
        (2.0, 0.45, 2.2),
        (3.01, 0.22, 3.1),
        (4.17, 0.12, 4.0),
        (5.43, 0.06, 5.5),
    ]
    out = sum(
        a * np.sin(2 * np.pi * f * r * t) * np.exp(-t * d) for r, a, d in partials
    )
    return out * np.minimum(1, t * SR / 60) * 0.35


def pad(
    midis: list[float], dur: float, attack: float = 0.6, release: float = 0.8
) -> np.ndarray:
    n = int((dur + release) * SR)
    t = t_axis(n)
    out = np.zeros(n)
    for m in midis:
        for det in (-0.07, 0.0, 0.07):
            f = hz(m + det)
            ph = rng.uniform(0, 2 * np.pi)
            saw = 2 * ((f * t + ph / (2 * np.pi)) % 1.0) - 1
            out += saw
    out = lp(out, 1400, 2)
    env = np.minimum(1, t / attack)
    env *= np.clip((dur + release - t) / release, 0, 1)
    return out * env / (3 * len(midis)) * 0.9


def bass_note(midi: float, dur: float) -> np.ndarray:
    n = int((dur + 0.08) * SR)
    t = t_axis(n)
    f = hz(midi)
    sig = (
        np.sin(2 * np.pi * f * t)
        + 0.35 * np.sin(4 * np.pi * f * t)
        + 0.12 * (2 * ((f * t) % 1) - 1)
    )
    sig = lp(sig, 520)
    env = (
        np.minimum(1, t * SR / 120)
        * np.clip((dur + 0.08 - t) / 0.08, 0, 1)
        * (0.75 + 0.25 * np.exp(-t * 6))
    )
    return sig * env * 0.55


def kick() -> np.ndarray:
    n = int(0.42 * SR)
    t = t_axis(n)
    f = 48 + 90 * np.exp(-t * 32)
    ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-t * 7.5)
    click = lp(rng.standard_normal(n), 3000) * np.exp(-t * 400) * 0.25
    return (body + click) * 0.9


def clap() -> np.ndarray:
    n = int(0.35 * SR)
    t = t_axis(n)
    noise = bp(rng.standard_normal(n), 900, 3200)
    env = np.zeros(n)
    for d in (0.0, 0.011, 0.022):
        env += np.where(t >= d, np.exp(-(t - d) * 90), 0)
    env += np.where(t >= 0.03, np.exp(-(t - 0.03) * 16), 0) * 0.6
    return noise * env * 0.35


def hat(open_: bool = False) -> np.ndarray:
    n = int((0.22 if open_ else 0.06) * SR)
    t = t_axis(n)
    noise = hp(rng.standard_normal(n), 7500, 4)
    return noise * np.exp(-t * (14 if open_ else 70)) * 0.22


def shaker() -> np.ndarray:
    n = int(0.09 * SR)
    t = t_axis(n)
    noise = bp(rng.standard_normal(n), 4500, 9500)
    env = np.sin(np.pi * np.clip(t / 0.09, 0, 1)) ** 2
    return noise * env * 0.18


def whoosh(
    dur: float, lo: float = 300, hi: float = 5000, up: bool = True
) -> np.ndarray:
    n = int(dur * SR)
    t = t_axis(n)
    noise = rng.standard_normal(n)
    out = np.zeros(n)
    steps = 24
    for s in range(steps):
        a, b = s * n // steps, (s + 1) * n // steps
        u = (s + 0.5) / steps
        fc = lo * (hi / lo) ** (u if up else 1 - u)
        out[a:b] = bp(noise, fc * 0.7, min(fc * 1.4, SR / 2 - 100))[a:b]
    env = np.sin(np.pi * np.clip(t / dur, 0, 1)) ** 1.5
    return out * env * 0.3


def sub_boom() -> np.ndarray:
    n = int(1.4 * SR)
    t = t_axis(n)
    f = 38 + 40 * np.exp(-t * 6)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 2.6) * 0.8


def reverb_ir(seconds: float = 1.8) -> np.ndarray:
    n = int(seconds * SR)
    t = t_axis(n)
    ir = rng.standard_normal((2, n)) * np.exp(-t * 6.9 / seconds)
    ir = lp(ir, 5200)
    ir[:, : int(0.012 * SR)] *= np.linspace(0, 1, int(0.012 * SR))
    return ir / np.sqrt(np.sum(ir**2, axis=1, keepdims=True))


# --- Score -------------------------------------------------------------------------

D, E, Fs, G, A, B, Cs = 62, 64, 66, 67, 69, 71, 73  # D4 major scale
CHORDS = [  # (start s, end s, root midi, chord name)
    (0.0, 3.0, D, "D"),
    (3.0, 5.0, D, "D"),
    (5.0, 7.0, B - 12, "Bm"),
    (7.0, 9.0, G - 12, "G"),
    (9.0, 11.0, A - 12, "A"),
    (11.0, 13.0, D, "D"),
    (13.0, 15.0, B - 12, "Bm"),
    (15.0, 16.5, G - 12, "G"),
    (16.5, 18.0, A - 12, "A"),
    (18.0, 21.0, D, "D"),
]
VOICING = {
    "D": [D - 12, A - 12, D, Fs, A, E + 12],
    "Bm": [B - 24, Fs - 12, B - 12, D, Fs, Cs + 12],
    "G": [G - 24, D - 12, G - 12, B - 12, D, A],
    "A": [A - 24, E - 12, A - 12, Cs, E, B],
}
ARP = {
    "D": [D + 12, Fs + 12, A + 12, E + 12, A + 12, Fs + 12, D + 24, A + 12],
    "Bm": [B, D + 12, Fs + 12, Cs + 12, Fs + 12, D + 12, B + 12, Fs + 12],
    "G": [G, B, D + 12, A + 12, D + 12, B, G + 12, D + 12],
    "A": [A, Cs + 12, E + 12, B + 12, E + 12, Cs + 12, A + 12, E + 12],
}
BASS_ROOT = {"D": D - 24, "Bm": B - 36, "G": G - 24, "A": A - 36}

music = Bus()
drums = Bus()
sfx = Bus()

for start, end, _root, name in CHORDS:
    first = start == 0.0
    music.add(
        pad(VOICING[name], end - start, attack=1.2 if first else 0.25),
        start,
        gain=0.55 if first else 0.42,
        rev=0.5,
    )
    # plucked arpeggio, 8th notes
    step = BEAT / 2
    k = 0
    tt = start
    while tt < min(end, 18.0) - 1e-6:
        if tt >= 0.25:
            g = 0.36 if tt < 3.0 else 0.32 if tt < 15.0 else 0.26
            accent = 1.0 if k % 2 == 0 else 0.7
            music.add(
                pluck(ARP[name][k % 8]),
                tt,
                gain=g * accent,
                pan=0.35 if k % 2 else -0.35,
                rev=0.35,
            )
        k += 1
        tt += step
    # bass from the downbeat, off-beat pulse with the root on the one
    if start >= 3.0 and start < 18.0:
        r = BASS_ROOT[name]
        b = start
        while b < end - 1e-6:
            beat_in_bar = round((b - 3.0) / BEAT) % 4
            dur = 0.42 if beat_in_bar == 0 else 0.2
            music.add(bass_note(r, dur), b, gain=0.7 if beat_in_bar == 0 else 0.45)
            if beat_in_bar in (1, 3):
                music.add(bass_note(r + 12, 0.12), b + BEAT / 2, gain=0.35)
            b += BEAT


# lead motif on the reveal and the proof (quarter = one beat)
def lead(midi: float, dur: float) -> np.ndarray:
    n = int((dur + 0.35) * SR)
    t = t_axis(n)
    f = hz(midi) * (
        1 + 0.004 * np.sin(2 * np.pi * 5.2 * t) * np.clip((t - 0.15) / 0.2, 0, 1)
    )
    ph = 2 * np.pi * np.cumsum(f) / SR
    sig = np.sin(ph) + 0.28 * np.sin(2 * ph) + 0.1 * np.sin(3 * ph)
    env = (
        np.clip(t / 0.03, 0, 1)
        * np.exp(-t * 1.1)
        * np.clip((dur + 0.35 - t) / 0.35, 0, 1)
    )
    return sig * env * 0.5


MOTIFS = [
    (
        3.0,
        [
            (D + 12, 1),
            (Fs + 12, 1),
            (A + 12, 2),
            (B + 12, 1),
            (A + 12, 1),
            (Fs + 12, 2),
        ],
    ),
    (
        11.0,
        [
            (A + 12, 1),
            (Fs + 12, 1),
            (D + 24, 2),
            (Cs + 24, 1),
            (B + 12, 1),
            (Fs + 12, 2),
        ],
    ),
]
for start, notes in MOTIFS:
    at = start
    for m, beats in notes:
        music.add(lead(m, beats * BEAT * 0.92), at, gain=0.2, pan=0.05, rev=0.55)
        at += beats * BEAT

# final chord + bell on the end card
music.add(bass_note(D - 24, 2.6), 18.0, gain=0.6)
sfx.add(bell(D + 24, 3.0), 18.02, gain=0.5, pan=-0.1, rev=0.6)
sfx.add(bell(A + 12, 3.0), 18.08, gain=0.3, pan=0.15, rev=0.6)

# intro: water shimmer (the loop filling) + riser into the downbeat
n = int(2.9 * SR)
t = t_axis(n)
water = bp(rng.standard_normal(n), 2200, 7000)
wob = 0.6 + 0.4 * np.sin(2 * np.pi * 7.3 * t) * np.sin(2 * np.pi * 2.1 * t + 1.1)
fill_env = np.clip(t / 0.4, 0, 1) * np.clip((2.9 - t) / 0.4, 0, 1)
sfx.add(water * wob * fill_env * 0.05, 0.15, pan=0.0, rev=0.4)
sfx.add(whoosh(1.25, 400, 9000, up=True), 1.78, gain=0.55, rev=0.3)
riser_t = t_axis(int(1.2 * SR))
riser = (
    np.sin(2 * np.pi * np.cumsum(hz(A) * 2 ** (riser_t / 1.2)) / SR)
    * (riser_t / 1.2) ** 2
    * 0.08
)
sfx.add(riser, 1.8, rev=0.4)
drums.add(sub_boom(), 3.0, gain=0.6)
sfx.add(
    hp(rng.standard_normal(int(1.2 * SR)), 3000)
    * np.exp(-t_axis(int(1.2 * SR)) * 3.5)
    * 0.08,
    3.0,
    rev=0.8,
)

# drums: groove 3-15 s, back for the build 16.5-18 s
b = 3.0
while b < 18.0 - 1e-6:
    beat_in_bar = round((b - 3.0) / BEAT) % 4
    groove = b < 15.0
    build = 16.5 <= b < 18.0
    if groove or build:
        drums.add(kick(), b, gain=0.55)
        if beat_in_bar in (1, 3):
            drums.add(clap(), b, gain=0.7, rev=0.35)
        drums.add(hat(), b + BEAT / 2, gain=0.8, pan=0.2)
        if beat_in_bar == 3:
            drums.add(hat(open_=True), b + BEAT / 2, gain=0.55, pan=0.2, rev=0.2)
    else:  # fable breakdown: shaker only
        drums.add(shaker(), b, gain=0.6, pan=-0.2, rev=0.2)
        drums.add(shaker(), b + BEAT / 2, gain=0.9, pan=0.2, rev=0.2)
    b += BEAT

# UI clicks (cursor): short tuned ticks
for at, m in ((8.22, A + 24), (8.6, D + 24), (9.15, Fs + 24)):
    sfx.add(pluck(m, 0.25, bright=2.2) * 0.9, at, gain=0.45, pan=-0.3, rev=0.25)
    sfx.add(
        hp(rng.standard_normal(int(0.012 * SR)), 2500) * 0.25, at, gain=0.6, pan=-0.3
    )
# transitions
for at in (6.62, 10.62, 14.62, 17.62):
    sfx.add(whoosh(0.5, 500, 6000, up=True), at, gain=0.45, rev=0.35)
# chart: the callout lands
sfx.add(pluck(A + 12, 0.6, 1.6), 13.38, gain=0.4, rev=0.4)
sfx.add(pluck(D + 24, 0.6, 1.6), 13.44, gain=0.35, rev=0.4)
# reveal: the wordmark
sfx.add(pluck(Fs + 24, 0.5, 1.8), 3.7, gain=0.3, pan=0.3, rev=0.4)

# --- Mix -------------------------------------------------------------------------

ir = reverb_ir()


def render(bus: Bus, gain: float) -> np.ndarray:
    wet = np.stack([fftconvolve(bus.send[c], ir[c])[:N] for c in range(2)])
    return (bus.dry + 0.45 * wet) * gain


mix = render(music, 1.0) + render(drums, 0.9) + render(sfx, 0.85)
mix = hp(mix, 28)
# gentle bus compression: soft knee via tanh on a normalized signal
peak = np.max(np.abs(mix))
mix = np.tanh(1.6 * mix / peak) / np.tanh(1.6)
fade_in = np.clip(t_axis(N) / 0.02, 0, 1)
fade_out = np.clip((DUR - t_axis(N)) / 1.2, 0, 1) ** 1.5
mix *= fade_in * fade_out
mix *= 10 ** (-1.0 / 20) / np.max(np.abs(mix))
rms = np.sqrt(np.mean(mix**2))
print(f"peak -1.0 dBFS, rms {20 * np.log10(rms):.1f} dBFS")
wavfile.write("soundtrack.wav", SR, mix.T.astype(np.float32))
