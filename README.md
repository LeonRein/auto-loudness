# auto-loudness

Loudness compensation (ISO 226:2023 equal-loudness contours) that follows the
hardware volume knob of a Focusrite Scarlett 4i4 4th Gen: the quieter you turn
the knob, the more the mids are lowered relative to bass and treble, so music keeps its balance at
low volume.

```
apps ─▶ "Loudness" sink (mono bass + room EQ per speaker + loudness) ─▶ Scarlett 4i4 ─▶ knob ─▶ speakers
                ▲                                                 │
                └──────────── auto-loudness reads ────────────────┘
```

- The knob is read from the ALSA control `Master HW Playback Volume`
  (scarlett2 kernel driver, read-only; needs MSD mode off).
- An optional fixed EQ from the config (`[[eq]]`, e.g. room correction) runs in
  the same filter: no EasyEffects needed, which saves one PipeWire cycle of
  latency (1.3 ms at a 64-sample quantum, 21 ms at the default 1024). Each band
  acts on both speakers or on one (`channel = "left"` / `"right"`), since room
  modes differ per speaker position.
- Optional mono bass (`mono_bass_hz`): below the crossover both speakers play
  left + right (Linkwitz-Riley 4th order, flat for mono content), which fills
  room dips that only one speaker has at the listening position.
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
systemctl --user start auto-loudness   # only now; from then on it starts with PipeWire
```

Then make **Loudness** the default sink (see Configure). The package enables the
service for every user (it starts with PipeWire); to opt out, run
`systemctl --user mask auto-loudness`.

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

## Filter curve

**Loudness** in the app menu (or `auto-loudness gui`) opens a window with the
response the service applies at the current knob position, loudness plus user
EQ (two curves, right dashed, when the speakers get different EQ), and follows
the knob while open. It only reads the config and the knob:
nothing in the audio path changes, and nothing runs while it is closed. It
needs `python-gobject`, `gtk4` and `libadwaita`. It shows the config as it is
on disk, so after editing it restart the service to make both agree.

## Panel icon

From the next login on, an icon in the top bar (GNOME needs the AppIndicator
extension; start it right away with `gio launch
/etc/xdg/autostart/auto-loudness-tray.desktop`) shows the state: bright
when **Loudness** is the default output, dimmed when another output is picked
and audio bypasses it, hidden when there is no Loudness sink. Its menu, or a
double click, opens the filter curve. It only wakes on sound server events, so
it uses no CPU while idle. Needs `python-gobject` and `libpulse` (for `pactl`).
It is a normal autostart entry, so it can be switched off per user in GNOME
Tweaks (Startup Applications).

## Develop

```sh
python3 -m unittest
python3 auto_loudness.py
```
