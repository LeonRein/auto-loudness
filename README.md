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
- The filter-chain never falls back to another output: if the 4i4 is missing,
  the Loudness sink stays silent instead of playing on the default device.

## Install

From the [leonrein paru repository](https://github.com/LeonRein/paru-repository):

```sh
paru -S auto-loudness
systemctl --user enable --now auto-loudness
```

Then send audio to the **Loudness** sink (e.g. set it as EasyEffects' output
device, or as the default sink).

## Calibrate

1. `journalctl --user -fu auto-loudness` shows the knob position in dB.
2. Turn the knob to where music sounds right at a loud listening level.
3. Put that value into `~/.config/auto-loudness/config.toml` and restart the
   service:

```toml
ref_knob_db = -20.0     # knob position for a flat response
low_hz = 40.0           # no extra bass below this
# card = "Scarlett 4i4 4th Gen"
# target = ""           # PipeWire sink node.name; "" = first sink of the card
```

## Develop

```sh
python3 -m unittest
python3 auto_loudness.py
```
