# auto-loudness

Loudness compensation (ISO 226:2023 equal-loudness contours) that follows the
hardware volume knob of a Focusrite Scarlett 4i4 4th Gen: the quieter you turn
the knob, the more the mids are lowered relative to bass and treble, so music keeps its balance at
low volume.

```
apps ─▶ EasyEffects (room EQ) ─▶ "Loudness" sink ─▶ Scarlett 4i4 ─▶ knob ─▶ speakers
                                      ▲                              │
                                      └──── auto-loudness reads ─────┘
```

- The knob is read from the ALSA control `Master HW Playback Volume`
  (scarlett2 kernel driver, read-only; needs MSD mode off).
- From the knob position it computes the ISO 226:2023 equal-loudness
  difference to the reference level and fits a 10-band EQ (PipeWire builtin
  biquads in a filter-chain that auto-loudness runs as a child process,
  updated live via `pw-cli`).
- **The EQ only cuts, it never boosts.** The bass stays at its original level
  and the mids/treble are lowered, so the output is never louder than without
  the tool. If the tool stops, the level can only rise to what the knob alone
  gives; at the reference position nothing changes at all.
- Below `low_hz` (default 40 Hz) no extra bass is added: small monitors can't
  play it, and it would only cost woofer excursion.
- The Loudness sink only ever plays into the 4i4: no fallback to the default
  sink, and it can't be moved in a mixer app. If its target output is
  missing, the sink stays silent instead of playing elsewhere.
- **Limit:** when the service stops, crashes, or restarts (unplug, profile
  change), the Loudness sink disappears and WirePlumber moves apps that were
  playing into it to the default sink, at their normal level, without
  compensation. Make the 4i4 the default sink so that this fallback goes where
  the speakers are.
- The compensation assumes the reference listening level stays fixed: after
  `calibrate`, leave the software volumes (apps, EasyEffects, sinks) alone and
  use only the knob.

## Install

From the [leonrein paru repository](https://github.com/LeonRein/paru-repository):

```sh
paru -S auto-loudness
systemctl --user enable --now auto-loudness
```

Then send audio to the **Loudness** sink (e.g. set it as EasyEffects' output
device, or as the default sink).

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
- Route audio through the **Loudness** sink (EasyEffects output device or
  default sink).
- Set the 4i4's PipeWire profile to stereo or Pro Audio.
- MSD mode off (new interfaces ship with it on; auto-loudness tells you how).

## Develop

```sh
python3 -m unittest
python3 auto_loudness.py
```
