#!/usr/bin/env python3
"""auto-loudness: ISO 226:2023 loudness compensation that follows the Scarlett volume knob.

Reads the interface's hardware volume knob via ALSA and drives a cut-only
equalizer in a PipeWire filter-chain that runs as a child process. The EQ
never boosts: the loudest band sits at 0 dB and the mids are lowered instead,
so the output can never be louder than with the tool switched off.
"""
import cmath
import functools
import json
import math
import os
import re
import select
import signal
import subprocess as sp
import sys
import time
import tomllib
from pathlib import Path

DEFAULTS = {
    "card": "Scarlett 4i4 4th Gen",           # name as in /proc/asound/cards
    "control": "Master HW Playback Volume",   # ALSA control that mirrors the knob
    "target": "",          # PipeWire sink node.name to play into; "" = first sink of `card`
    "ref_knob_db": 0.0,    # knob position (dB) at which music sounds "right" -> flat response
    "low_hz": 40.0,        # no extra bass below this (speaker limit; saves woofer excursion)
    "strength": 1.0,       # 1 = full ISO 226 compensation, smaller = gentler, 0 = off
    "mono_bass_hz": 0.0,   # 0 = off; else both speakers play L+R below this crossover (LR4)
    "eq": [],              # fixed user EQ (e.g. room correction): [[eq]] tables, see config.example.toml
}
EQ_TYPES = ("peaking", "lowshelf", "highshelf", "lowpass", "highpass")
EQ_CHANNELS = ("both", "left", "right")
CHANNELS = (("L", "left"), ("R", "right"))  # graph prefix, [[eq]] channel name
MONO_BASS_RANGE = (40.0, 250.0)
LR4_Q = 1 / math.sqrt(2)  # Linkwitz-Riley 4th order = two Butterworth 2nd-order stages
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "auto-loudness/config.toml"
SINK_NAME = "auto_loudness"

# ISO 226:2023 Table 1
ISO_F = [20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
         1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500]
ISO_AF = [0.635, 0.602, 0.569, 0.537, 0.509, 0.482, 0.456, 0.433, 0.412, 0.391, 0.373, 0.357,
          0.343, 0.330, 0.320, 0.311, 0.303, 0.300, 0.295, 0.292, 0.290, 0.290, 0.289, 0.289,
          0.289, 0.293, 0.303, 0.323, 0.354]
ISO_LU = [-31.5, -27.2, -23.1, -19.3, -16.1, -13.1, -10.4, -8.2, -6.3, -4.6, -3.2, -2.1, -1.2,
          -0.5, 0.0, 0.4, 0.5, 0.0, -2.7, -4.2, -1.2, 1.4, 2.3, 1.0, -2.3, -7.2, -11.2, -10.9, -3.5]
ISO_TF = [78.1, 68.7, 59.5, 51.1, 44.0, 37.5, 31.5, 26.5, 22.1, 17.9, 14.4, 11.4, 8.6, 6.2, 4.4,
          3.0, 2.2, 2.4, 3.5, 1.7, -1.3, -4.2, -6.0, -5.4, -1.5, 6.0, 12.6, 13.9, 12.3]
REF_PHON = 80.0   # listening level at ref_knob_db; the curve shape barely depends on it
MIN_PHON = 20.0   # ISO 226 is defined from 20 phon up

# Half-octave peaking bands, Q 1.0: overlapping, the least-squares fit handles the overlap.
# Octave bands rippled ±0.7 dB between the ISO points when turned down far; these ±0.2 dB.
# The top band is a high shelf so the cut holds up to 20 kHz like the norm's 12.5 kHz value
# (a peak there springs back, up to +10 dB at 20 kHz).
BANDS = [("peaking", round(31.5 * 2 ** (i / 2), 1), 1.0) for i in range(18)] + [("highshelf", 14000, 0.7)]
RATE = 48000  # shortcut: fit assumes 48 kHz; at 44.1 kHz the top band shifts slightly, harmless


def contour(phon):
    """ISO 226:2023 formula (1): SPL in dB at each ISO_F for the given loudness level."""
    return [10 / a * math.log10(4e-10 ** (0.3 - a) * (10 ** (0.03 * phon) - 10 ** 0.072)
                                + 10 ** (a * (t + u) / 10)) - u
            for a, u, t in zip(ISO_AF, ISO_LU, ISO_TF)]


def shape(phon, low_hz, strength=1.0):
    """Cut-only gain (dB) at each ISO_F that turns the reference balance into the one at `phon`."""
    raw = [(c - phon) - (r - REF_PHON) for c, r in zip(contour(phon), contour(REF_PHON))]
    floor = interp(low_hz, ISO_F, raw)
    raw = [floor if f < low_hz else g for f, g in zip(ISO_F, raw)]
    top = max(raw)
    return [(g - top) * strength for g in raw]


def target_gains(knob_rel, low_hz=40.0, strength=1.0):
    """Knob offset from reference (dB, <= 0) -> cut-only gain (dB) at each ISO_F, max 0."""
    # The cut at 1 kHz lowers the real listening level below REF_PHON + knob_rel, which needs
    # a stronger contour: bisect for the level where both agree (monotonic, clamped to 20 phon).
    if knob_rel >= 0:
        return [0.0] * len(ISO_F)
    lo, hi = MIN_PHON, REF_PHON
    for _ in range(30):
        mid = (lo + hi) / 2
        if mid > REF_PHON + knob_rel + shape(mid, low_hz, strength)[ISO_F.index(1000)]:
            hi = mid
        else:
            lo = mid
    return shape(lo, low_hz, strength)


def interp(f, xs, ys):
    if f <= xs[0]:
        return ys[0]
    if f >= xs[-1]:
        return ys[-1]
    i = next(i for i, x in enumerate(xs) if x >= f)
    t = math.log(f / xs[i - 1]) / math.log(xs[i] / xs[i - 1])
    return ys[i - 1] + t * (ys[i] - ys[i - 1])


def biquad_db(f, kind, f0, q, gain=0.0):
    """Magnitude (dB) at f of PipeWire's bq_<kind> (RBJ cookbook, same formulas as its biquad.c)."""
    return 20 * math.log10(max(abs(biquad_h(f, kind, f0, q, gain)), 1e-12))


def biquad_h(f, kind, f0, q, gain=0.0):
    """Complex response at f of PipeWire's bq_<kind>."""
    b, d = coeffs(kind, f0, q, gain)
    z = cmath.exp(-1j * 2 * math.pi * f / RATE)
    return (b[0] + b[1] * z + b[2] * z * z) / (d[0] + d[1] * z + d[2] * z * z)


@functools.lru_cache(maxsize=None)
def _phis(freqs):
    return tuple(math.sin(math.pi * f / RATE) ** 2 for f in freqs)


def biquad_db_many(freqs, kind, f0, q, gain=0.0):
    """biquad_db at many frequencies (a tuple) at once: closed-form |H|^2 in sin^2(w/2), real
    arithmetic only, several times faster than the complex evaluation point by point."""
    def terms(c):
        return (c[0] + c[1] + c[2]) ** 2, 4 * (c[0] * c[1] + 4 * c[0] * c[2] + c[1] * c[2]), 16 * c[0] * c[2]
    b, d = coeffs(kind, f0, q, gain)
    (n0, n1, n2), (d0, d1, d2) = terms(b), terms(d)
    return [10 * math.log10(max((n0 - n1 * p + n2 * p * p) / (d0 - d1 * p + d2 * p * p), 1e-24))
            for p in _phis(freqs)]


@functools.lru_cache(maxsize=64)
def _fixed_db(freqs, bands):
    """Summed response (dB) of fixed bands ((type, freq, q, gain), ...) at freqs, cached: the
    user EQ never changes while the service or the window runs."""
    total = [0.0] * len(freqs)
    for b in bands:
        total = [t + x for t, x in zip(total, biquad_db_many(freqs, *b))]
    return tuple(total)


def response_many(freqs, gains, eq=()):
    """response() at many frequencies at once."""
    freqs = tuple(freqs)
    total = list(_fixed_db(freqs, tuple((b["type"], b["freq"], b["q"], b["gain"]) for b in eq)))
    for (kind, f0, q), g in zip(BANDS, gains):
        if g:  # a band at 0 dB is exactly flat
            total = [t + x for t, x in zip(total, biquad_db_many(freqs, kind, f0, q, g))]
    return total


def coeffs(kind, f0, q, gain=0.0):
    """RBJ biquad coefficients (b, a), as PipeWire's biquad.c computes them."""
    w0 = 2 * math.pi * f0 / RATE
    alpha, k, a = math.sin(w0) / (2 * q), math.cos(w0), 10 ** (gain / 40)
    k2 = 2 * math.sqrt(a) * alpha
    return {
        "peaking": ((1 + alpha * a, -2 * k, 1 - alpha * a), (1 + alpha / a, -2 * k, 1 - alpha / a)),
        "lowshelf": ((a * (a + 1 - (a - 1) * k + k2), 2 * a * (a - 1 - (a + 1) * k), a * (a + 1 - (a - 1) * k - k2)),
                     (a + 1 + (a - 1) * k + k2, -2 * (a - 1 + (a + 1) * k), a + 1 + (a - 1) * k - k2)),
        "highshelf": ((a * (a + 1 + (a - 1) * k + k2), -2 * a * (a - 1 + (a + 1) * k), a * (a + 1 + (a - 1) * k - k2)),
                      (a + 1 - (a - 1) * k + k2, 2 * (a - 1 - (a + 1) * k), a + 1 - (a - 1) * k - k2)),
        "lowpass": (((1 - k) / 2, 1 - k, (1 - k) / 2), (1 + alpha, -2 * k, 1 - alpha)),
        "highpass": (((1 + k) / 2, -(1 + k), (1 + k) / 2), (1 + alpha, -2 * k, 1 - alpha)),
    }[kind]


def mono_bass_h(f, fc, own=1.0, other=0.0):
    """Complex output of one channel's mono-bass split for inputs own/other:
    own through the LR4 high pass, plus (own + other) / 2 through the LR4 low pass."""
    lp = biquad_h(f, "lowpass", fc, LR4_Q) ** 2
    hp = biquad_h(f, "highpass", fc, LR4_Q) ** 2
    return hp * own + lp * (own + other) / 2


def channel_eq(eq, channel):
    """The [[eq]] bands that act on one speaker ('left' or 'right')."""
    return [b for b in eq if b["channel"] in ("both", channel)]


def response(f, gains, eq=()):
    """Total response (dB): loudness bands plus the fixed user EQ (of one channel)."""
    return (sum(biquad_db(f, kind, f0, q, g) for (kind, f0, q), g in zip(BANDS, gains))
            + sum(biquad_db(f, b["type"], b["freq"], b["q"], b["gain"]) for b in eq))


GRID = [20 * 1000 ** (i / 999) for i in range(1000)]  # 20 Hz .. 20 kHz, 1/100 octave
FIT_PTS = tuple(GRID[::10])  # 1/10 octave: where the loudness fit is evaluated


@functools.lru_cache(maxsize=None)
def _fit_basis():
    """Jacobian of the loudness bands at FIT_PTS (dB per dB at 1 dB) and its normal matrix."""
    cols = [biquad_db_many(FIT_PTS, kind, f0, q, 1.0) for kind, f0, q in BANDS]
    m = [list(row) for row in zip(*cols)]
    n = len(BANDS)
    ata = [[sum(row[i] * row[j] for row in m) + (1e-3 if i == j else 0) for j in range(n)] for i in range(n)]
    return m, ata


def solve(a, b):
    """Gaussian elimination with partial pivoting (small dense system)."""
    n = len(b)
    a = [row[:] + [x] for row, x in zip(a, b)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        a[c], a[p] = a[p], a[c]
        for r in range(c + 1, n):
            k = a[r][c] / a[c][c]
            a[r] = [x - k * y for x, y in zip(a[r], a[c])]
    x = [0.0] * n
    for r in reversed(range(n)):
        x[r] = (a[r][n] - sum(a[r][j] * x[j] for j in range(r + 1, n))) / a[r][r]
    return x


def eq_params(knob_db, cfg):
    """Knob position -> (band gains in dB, linear output gain <= 1)."""
    target = target_gains(min(0.0, knob_db - cfg["ref_knob_db"]), cfg["low_hz"],
                          min(1.0, max(0.0, cfg["strength"])))
    # fit the whole line through the ISO points (1/10 octave steps), not just the points, so it
    # doesn't ripple between them; hold the 12.5 kHz value above
    pts = FIT_PTS
    want = [interp(f, ISO_F + [16000], target + [target[-1]]) for f in pts]
    # least squares on the overlapping bands; a few Gauss-Newton steps fix the slight non-linearity
    m, ata = _fit_basis()
    n, gains = len(BANDS), [0.0] * len(BANDS)
    for _ in range(5):
        r = [w - x for w, x in zip(want, response_many(pts, gains))]
        atr = [sum(row[i] * x for row, x in zip(m, r)) for i in range(n)]
        gains = [g + d for g, d in zip(gains, solve(ata, atr))]
    # one output gain for both channels (keeps the balance); the mono-bass split never boosts
    eq = cfg["eq"]
    freqs = tuple(GRID + [b["freq"] for b in eq])
    loud = response_many(freqs, gains)
    peak = max(x + u for _, ch in CHANNELS for x, u in zip(loud, response_many(freqs, [], channel_eq(eq, ch))))
    if eq or any(abs(g) > 1e-6 for g in gains):
        peak += 0.01  # margin for peaks between grid points
    return gains, 10 ** (-max(0.0, peak) / 20)  # guarantees the whole response stays <= 0 dB


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        with CONFIG.open("rb") as f:
            user = tomllib.load(f)
        unknown = user.keys() - DEFAULTS.keys()
        if unknown:
            sys.exit(f"{CONFIG}: unknown keys {sorted(unknown)}")
        user["eq"] = check_eq(user.get("eq", []))
        for k, v in user.items():
            if k == "eq":
                continue
            want_str = isinstance(DEFAULTS[k], str)
            if (isinstance(v, str) != want_str) or isinstance(v, bool) or not isinstance(v, (str, int, float)):
                sys.exit(f"{CONFIG}: {k} must be {'a string' if want_str else 'a number'}")
        if any(c in user.get("target", "") for c in '"\\'):
            sys.exit(f"{CONFIG}: target must not contain quotes or backslashes")
        mono = user.get("mono_bass_hz", 0)
        if mono and not MONO_BASS_RANGE[0] <= mono <= MONO_BASS_RANGE[1]:
            sys.exit(f"{CONFIG}: mono_bass_hz must be 0 (off) or from {MONO_BASS_RANGE[0]:.0f} to "
                     f"{MONO_BASS_RANGE[1]:.0f}")
        cfg.update(user)
    return cfg


def check_eq(eq):
    """Validate the [[eq]] tables and fill in defaults (q 0.707, gain 0)."""
    def num(b, i, key, lo, hi, default):
        v = b.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            sys.exit(f"{CONFIG}: eq #{i + 1}: {key} must be a number from {lo} to {hi}")
        return float(v)

    if not isinstance(eq, list) or not all(isinstance(b, dict) for b in eq):
        sys.exit(f"{CONFIG}: eq must be a list of [[eq]] tables")
    keys = {"type", "freq", "q", "gain", "channel"}
    out = []
    for i, b in enumerate(eq):
        if b.keys() - keys:
            sys.exit(f"{CONFIG}: eq #{i + 1}: unknown keys {sorted(b.keys() - keys)}")
        if b.get("type") not in EQ_TYPES:
            sys.exit(f"{CONFIG}: eq #{i + 1}: type must be one of {', '.join(EQ_TYPES)}")
        if b["type"] in ("lowpass", "highpass") and "gain" in b:
            sys.exit(f"{CONFIG}: eq #{i + 1}: {b['type']} has no gain")
        if b.get("channel", "both") not in EQ_CHANNELS:
            sys.exit(f"{CONFIG}: eq #{i + 1}: channel must be one of {', '.join(EQ_CHANNELS)}")
        out.append({"type": b["type"], "freq": num(b, i, "freq", 10, 20000, None),
                    "q": num(b, i, "q", 0.1, 10, 0.707), "gain": num(b, i, "gain", -24, 24, 0.0),
                    "channel": b.get("channel", "both")})
    return out


def find_card(name):
    for line in Path("/proc/asound/cards").read_text().splitlines():
        m = re.match(r"\s*(\d+) \[.*\]: .* - (.*)$", line)
        if m and m[2].strip() == name:
            return int(m[1])
    return None


def parse_amixer(out):
    """amixer cget output -> knob position in dB, using the control's own dB range."""
    rng = re.search(r"min=(-?\d+),max=(-?\d+)", out)
    db = re.search(r"dBminmax-min=(-?[\d.]+)dB,max=(-?[\d.]+)dB", out)
    val = re.search(r": values=(-?\d+)", out)
    if not (rng and db and val):
        sys.exit(f"unexpected amixer output (no dB range?):\n{out}")
    vmin, vmax = int(rng[1]), int(rng[2])
    return float(db[1]) + (int(val[1]) - vmin) / (vmax - vmin) * (float(db[2]) - float(db[1]))


def read_knob(card, control):
    """Knob position in dB, or None if the control can't be read (unplugged, MSD mode)."""
    try:
        out = sp.check_output(["amixer", "-c", str(card), "cget", f"name={control}"], text=True,
                              stderr=sp.DEVNULL)
    except sp.CalledProcessError:
        return None
    return parse_amixer(out)


def knob_error(card, control):
    return (f"cannot read {control!r} on card {card}: device unplugged, or MSD mode still on "
            f"(fix: amixer -c {card} cset name='MSD Mode Switch' off, then power-cycle the interface)")


def calibrate():
    """Store the current knob position as the reference (flat) position."""
    cfg = load_config()
    card = find_card(cfg["card"])
    if card is None:
        sys.exit(f"sound card {cfg['card']!r} not found")
    knob = read_knob(card, cfg["control"])
    if knob is None:
        sys.exit(knob_error(card, cfg["control"]))
    line = f"ref_knob_db = {knob:.1f}"
    text = CONFIG.read_text() if CONFIG.exists() else ""
    if re.search(r"(?m)^ref_knob_db\s*=", text):
        text = re.sub(r"(?m)^ref_knob_db\s*=.*$", line, text)
    else:
        text += ("\n" if text and not text.endswith("\n") else "") + line + "\n"
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(text)
    print(f"{CONFIG}: {line}")
    sp.run(["systemctl", "--user", "try-restart", "auto-loudness.service"])


def pw_nodes():
    return {o["info"]["props"].get("node.name"): o for o in json.loads(sp.check_output(["pw-dump"]))
            if o.get("type") == "PipeWire:Interface:Node"}


def find_target(card_name, nodes):
    """First PipeWire sink of the card, or None. Its name changes with the card profile."""
    prefix = "alsa_output.usb-Focusrite_" + card_name.replace(" ", "_")
    for name, o in nodes.items():
        if name and name.startswith(prefix) and o["info"]["props"].get("media.class") == "Audio/Sink":
            return name
    return None


def filter_chain_conf(target, eq, gains, mult, mono_bass_hz=0.0):
    """Graph per channel (L, R): input -> [mono-bass split] -> user EQ of that speaker ->
    two identical loudness chains a/b -> mixer.

    Mono bass: each channel = own signal through an LR4 high pass + (L + R) / 2 through an
    LR4 low pass, so below the crossover both speakers play the same bass; for mono content
    the split sums flat (LR4 low + high pass = all pass).

    PipeWire's biquads reset their state on every parameter change (audible click), so new
    values go to the silent chain, which is then crossfaded in (see Fader)."""
    def bq(name, kind, f, q, g=0.0):
        return (f'          {{ type = builtin name = {name} label = bq_{kind} '
                f'control = {{ "Freq" = {f} "Q" = {q} "Gain" = {g:.3f} }} }}')

    def mixer(name, *gains_):
        ctl = " ".join(f'"Gain {i + 1}" = {g:.6f}' for i, g in enumerate(gains_))
        return f'          {{ type = builtin name = {name} label = mixer control = {{ {ctl} }} }}'

    def link(a, b, port="In"):
        return f'          {{ output = "{a}:Out" input = "{b}:{port}" }}'

    nodes, links, last = [], [], len(BANDS) - 1
    if mono_bass_hz:
        nodes.append(mixer("mono", 0.5, 0.5))
    for ch, name in CHANNELS:
        nodes.append(f"          {{ type = builtin name = in{ch} label = copy }}")
        head = f"in{ch}"
        if mono_bass_hz:
            for kind, p in (("lowpass", "lp"), ("highpass", "hp")):
                nodes += [bq(f"{p}{ch}{s}", kind, mono_bass_hz, LR4_Q) for s in (1, 2)]
                links += [link(head, f"{p}{ch}1"), link(f"{p}{ch}1", f"{p}{ch}2")]
            nodes.append(mixer(f"x{ch}", 1.0, 1.0))
            links += [link(f"lp{ch}2", "mono", f"In {1 if ch == 'L' else 2}"),
                      link(f"hp{ch}2", f"x{ch}", "In 1"), link("mono", f"x{ch}", "In 2")]
            head = f"x{ch}"
        for i, b in enumerate(channel_eq(eq, name)):
            nodes.append(bq(f"u{ch}{i}", b["type"], b["freq"], b["q"], b["gain"]))
            links.append(link(head, f"u{ch}{i}"))
            head = f"u{ch}{i}"
        for c in "ab":
            nodes += [bq(f"{c}{ch}{i}", kind, f, q, g) for i, ((kind, f, q), g) in enumerate(zip(BANDS, gains))]
            links.append(link(head, f"{c}{ch}0"))
            links += [link(f"{c}{ch}{i}", f"{c}{ch}{i + 1}") for i in range(last)]
        nodes.append(mixer(f"mix{ch}", mult, 0.0))
        links += [link(f"a{ch}{last}", f"mix{ch}", "In 1"), link(f"b{ch}{last}", f"mix{ch}", "In 2")]
    nodes, links = "\n".join(nodes), "\n".join(links)
    return f"""
context.spa-libs = {{ audio.convert.* = audioconvert/libspa-audioconvert support.* = support/libspa-support }}
context.modules = [
  {{ name = libpipewire-module-rt flags = [ ifexists nofail ] }}
  {{ name = libpipewire-module-protocol-native }}
  {{ name = libpipewire-module-client-node }}
  {{ name = libpipewire-module-adapter }}
  {{ name = libpipewire-module-filter-chain
    args = {{
      node.description = "Loudness"
      filter.graph = {{
        nodes = [
{nodes}
        ]
        links = [
{links}
        ]
        inputs = [ "inL:In" "inR:In" ]
        outputs = [ "mixL:Out" "mixR:Out" ]
      }}
      audio.channels = 2
      audio.position = [ FL FR ]
      capture.props  = {{ node.name = "{SINK_NAME}" media.class = Audio/Sink }}
      # Only ever play into the target: no fallback to the default sink, no moving it in a
      # mixer app, and if the target is missing, wait silently instead of being destroyed.
      playback.props = {{ node.name = "{SINK_NAME}.out" node.passive = true
                          target.object = "{target}" node.dont-fallback = true
                          node.linger = true node.dont-move = true }}
    }}
  }}
]
"""


def log(knob, gains, mult, eq):
    db = 20 * math.log10(mult)
    points = (("40 Hz", 40), ("1 kHz", 1000), ("10 kHz", 10000))
    per_channel = any(b["channel"] != "both" for b in eq)
    parts = [(f"{ch} " if per_channel else "") + ", ".join(
        f"{name} {response(f, gains, channel_eq(eq, cname)) + db:+.1f} dB" for name, f in points)
        for ch, cname in (CHANNELS if per_channel else CHANNELS[:1])]
    print(f"knob {knob:+.1f} dB -> " + " | ".join(parts), flush=True)


class Fader:
    """Click-free updates: retune the silent chain, let it settle, then crossfade to it."""
    SETTLE = 0.08  # s; lowest band (31.5 Hz, Q 1.0) decays in ~10 ms, plus a quantum or two
    FADE, STEPS = 0.3, 60  # measured: clicks ~-75 dBFS (hard switch: -47 dBFS)

    def __init__(self, node_id, mult):
        self.node, self.active, self.mult = node_id, "a", mult
        self.cli = sp.Popen(["pw-cli"], stdin=sp.PIPE, stdout=sp.DEVNULL, stderr=sp.DEVNULL, text=True)

    def send(self, params):
        self.cli.stdin.write(f"set-param {self.node} Props {{ params = [ {params} ] }}\n")
        self.cli.stdin.flush()

    def apply(self, gains, mult):
        idle = "b" if self.active == "a" else "a"
        self.send(" ".join(f'"{idle}{ch}{i}:Gain" {g:.3f}' for ch, _ in CHANNELS for i, g in enumerate(gains)))
        time.sleep(self.SETTLE)
        old_in, new_in = ("1", "2") if self.active == "a" else ("2", "1")
        for step in range(1, self.STEPS + 1):
            f = step / self.STEPS
            self.send(" ".join(f'"mix{ch}:Gain {old_in}" {(1 - f) * self.mult:.6f} '
                               f'"mix{ch}:Gain {new_in}" {f * mult:.6f}' for ch, _ in CHANNELS))
            time.sleep(self.FADE / self.STEPS)
        self.active, self.mult = idle, mult

    def close(self):
        self.cli.terminate()
        self.cli.wait(timeout=5)


def run(cfg, card, target, knob):
    """Run the Loudness sink until the 4i4 or its output goes away; returns the reason."""
    gains, mult = eq_params(knob, cfg)
    conf = Path(os.environ["XDG_RUNTIME_DIR"]) / "auto-loudness.conf"
    conf.write_text(filter_chain_conf(target, cfg["eq"], gains, mult, cfg["mono_bass_hz"]))
    chain = monitor = fader = None
    try:
        chain = sp.Popen(["pipewire", "-c", str(conf)])
        monitor = sp.Popen(["alsactl", "monitor", f"hw:{card}"], stdout=sp.PIPE, stderr=sp.DEVNULL)
        for _ in range(50):
            node = pw_nodes().get(SINK_NAME)
            if node:
                break
            time.sleep(0.1)
        else:
            sys.exit("filter-chain sink did not appear")
        mono = f", mono bass below {cfg['mono_bass_hz']:.0f} Hz" if cfg["mono_bass_hz"] else ""
        print(f"card {card}, target {target}, {len(cfg['eq'])} user EQ bands{mono}", flush=True)
        log(knob, gains, mult, cfg["eq"])
        fader = Fader(node["id"], mult)
        logged, changed = knob, 0.0

        while chain.poll() is None:
            # events give instant updates; the 1 s timeout is a polling fallback
            if select.select([monitor.stdout], [], [], 1.0)[0]:
                if not os.read(monitor.stdout.fileno(), 4096):
                    return "4i4 disconnected"
            nodes = pw_nodes()
            # the pipewire child keeps running even when its filter-chain module is gone
            if SINK_NAME not in nodes:
                sys.exit("Loudness sink vanished")
            # output gone (profile off, unplug) or renamed (profile change): drop the sink so
            # PipeWire picks an output as if this tool didn't exist; come back with the 4i4
            if target not in nodes or (cfg["target"] or find_target(cfg["card"], nodes)) != target:
                return f"output {target} gone"
            new = read_knob(card, cfg["control"])
            if new is None:
                return "4i4 disconnected"
            if new != knob:
                knob = new
                gains, mult = eq_params(knob, cfg)
                fader.apply(gains, mult)
                changed = time.monotonic()
            # log only where the knob comes to rest, not every step of a turn
            if knob != logged and time.monotonic() - changed >= 1.0:
                logged = knob
                log(knob, gains, mult, cfg["eq"])
        sys.exit(f"filter-chain exited with {chain.returncode}")
    finally:
        if fader:
            fader.close()
        for p in (monitor, chain):
            if p:
                p.terminate()
                p.wait(timeout=5)


def main():
    if sys.argv[1:] == ["calibrate"]:
        return calibrate()
    if sys.argv[1:] == ["gui"]:
        import auto_loudness_gui  # GTK only loads here, never in the service
        return auto_loudness_gui.main()
    if sys.argv[1:] == ["tray"]:
        import auto_loudness_tray
        return auto_loudness_tray.main()
    if sys.argv[1:]:
        sys.exit("usage: auto-loudness [calibrate | gui | tray]")
    cfg = load_config()
    if "XDG_RUNTIME_DIR" not in os.environ:
        sys.exit("XDG_RUNTIME_DIR not set: run as a systemd user service or in a login session")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    waiting, misses = False, 0
    while True:
        # without the 4i4 there is no Loudness sink at all: PipeWire behaves as without this tool
        card = find_card(cfg["card"])
        nodes = pw_nodes()
        target = cfg["target"] or find_target(cfg["card"], nodes)
        if card is None or not target or target not in nodes:
            if not waiting:
                print(f"waiting for {cfg['card']} and its PipeWire output", flush=True)
                waiting = True
            time.sleep(1)
            continue
        waiting = False
        knob = read_knob(card, cfg["control"])
        if knob is None:  # may just be an unplug in progress; persistent means MSD mode
            misses += 1
            if misses >= 5:
                sys.exit(knob_error(card, cfg["control"]))
            time.sleep(1)
            continue
        misses = 0
        print(run(cfg, card, target, knob), flush=True)


if __name__ == "__main__":
    main()
