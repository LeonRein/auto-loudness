# auto-loudness

Loudness compensation (ISO 226:2023 equal-loudness contours) that follows the
hardware volume knob of a Focusrite Scarlett 4i4 4th Gen: the quieter you turn
the knob, the more the mids are lowered relative to bass and treble, so music keeps its balance at
low volume.

```
apps ─▶ "Loudness" sink (room EQ + loudness) ─▶ Scarlett 4i4 ─▶ knob ─▶ speakers
                ▲                                                 │
                └──────────── auto-loudness reads ────────────────┘
```

- The knob is read from the ALSA control `Master HW Playback Volume`
  (scarlett2 kernel driver, read-only; needs MSD mode off).
- An optional fixed EQ from the config (`[[eq]]`, e.g. room correction) runs in
  the same filter: no EasyEffects needed, which saves one PipeWire cycle of
  latency (1.3 ms at a 64-sample quantum, 21 ms at the default 1024).
- From the knob position it computes the ISO 226:2023 equal-loudness
  difference to the reference level and fits a 10-band EQ (PipeWire builtin
  biquads in a filter-chain that auto-loudness runs as a child process,
  updated live via `pw-cli`).
- **The total response never exceeds 0 dB.** The loudness part only cuts (bass
  stays, mids/treble are lowered), and boosts in the user EQ are offset by a
  lower output gain, so the output is never louder than without the tool. If the tool stops, the level can only rise to what the knob alone
  gives; at the reference position nothing changes at all.
- Below `low_hz` (default 40 Hz) no extra bass is added: small monitors can't
  play it, and it would only cost woofer excursion.
- The Loudness sink exists only while the 4i4 and its output exist, and it
  only ever plays into the 4i4 (no fallback to another output, can't be moved
  in a mixer app). Unplug the 4i4 or switch its profile off, and the sink is
  removed within a second, so PipeWire picks an output exactly as it would
  without this tool; plug it back in and the sink returns.
- When the service stops, the sink disappears the same way.
- The compensation assumes the reference listening level stays fixed: after
  `calibrate`, leave the software volumes (apps, EasyEffects, sinks) alone and
  use only the knob.

## Install

From the [leonrein paru repository](https://github.com/LeonRein/paru-repository):

```sh
paru -S auto-loudness
systemctl --user enable --now auto-loudness
```

Then make **Loudness** the default sink (see Configure).

## Configure

Turn the knob to where music sounds right at a loud listening level, then:

```sh
auto-loudness calibrate
```

This stores the knob position as `ref_knob_db` in
`~/.config/auto-loudness/config.toml` (flat response there, compensation
below) and restarts the service. `journalctl --user -fu auto-loudness` shows
what it does. All options, with defaults, are in
[`config.example.toml`](config.example.toml) (installed to
`/usr/share/doc/auto-loudness/`); after editing the file run
`systemctl --user restart auto-loudness`.

Also needed once:
- Route audio through the **Loudness** sink: make it the default sink, or
  select it as EasyEffects' output device if you keep EasyEffects.
- Set the 4i4's PipeWire profile to stereo or Pro Audio.
- MSD mode off (new interfaces ship with it on; auto-loudness tells you how).

## Develop

```sh
python3 -m unittest
python3 auto_loudness.py
```
