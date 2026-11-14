import math
import re
import unittest

from unittest import mock

import auto_loudness
from auto_loudness import (DEFAULTS, GRID, ISO_F, biquad_db, biquad_db_many, channel_eq, check_eq, contour,
                           eq_params, filter_chain_conf, interp, mono_bass_h, parse_amixer, response,
                           response_many, target_gains)

AMIXER = """numid=6,iface=MIXER,name='Master HW Playback Volume'
  ; type=INTEGER,access=r----R--,values=1,min=0,max=127,step=1
  : values=27
  | dBminmax-min=-127.00dB,max=0.00dB
"""
CFG = dict(DEFAULTS, ref_knob_db=-20)


class Test(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_amixer(AMIXER), -100.0)

    def test_parse_failed_read(self):
        # the driver failed to read the element: amixer prints only the header and exits 0
        self.assertIsNone(parse_amixer("\n".join(AMIXER.splitlines()[:2])))

    def test_read_knob_retries_glitch(self):
        bad = mock.Mock(returncode=0, stdout="\n".join(AMIXER.splitlines()[:2]), stderr="")
        good = mock.Mock(returncode=0, stdout=AMIXER, stderr="")
        with mock.patch.object(auto_loudness.sp, "run", side_effect=[bad, good]), \
             mock.patch.object(auto_loudness.time, "sleep"):
            self.assertEqual(auto_loudness.read_knob(2, "Master HW Playback Volume"), -100.0)

    def test_read_knob_gives_up(self):
        gone = mock.Mock(returncode=1, stdout="", stderr="amixer: Invalid card number '2'\n")
        with mock.patch.object(auto_loudness.sp, "run", return_value=gone) as run, \
             mock.patch.object(auto_loudness.time, "sleep"), mock.patch("sys.stderr"):
            self.assertIsNone(auto_loudness.read_knob(2, "Master HW Playback Volume"))
        self.assertEqual(run.call_count, 3)

    def test_iso_1khz_is_identity(self):
        for phon in (20, 40, 60, 80):
            self.assertAlmostEqual(contour(phon)[ISO_F.index(1000)], phon)

    def test_flat_at_and_above_reference(self):
        for knob in (-20, 0):
            gains, mult = eq_params(knob, CFG)
            self.assertTrue(all(abs(g) < 1e-6 for g in gains))
            self.assertEqual(mult, 1.0)

    def test_never_boosts(self):
        for knob in (-30, -50, -80, -127):
            gains, mult = eq_params(knob, CFG)
            gain_db = 20 * math.log10(mult)
            self.assertLessEqual(max(response(f, gains) for f in GRID) + gain_db, 1e-9)

    def test_fit_follows_iso(self):
        target = target_gains(-40, CFG["low_hz"])
        self.assertAlmostEqual(max(target), 0.0)
        self.assertLess(target[ISO_F.index(1000)], -15)  # mids lowered, bass kept
        gains, mult = eq_params(-60, CFG)
        gain_db = 20 * math.log10(mult)
        err = max(abs(response(f, gains) + gain_db - interp(f, ISO_F, target))
                  for f in ISO_F if 31.5 <= f <= 12500)
        self.assertLess(err, 1.5)
        top = max(response(f, gains) + gain_db for f in GRID if f > 12500)
        self.assertLess(top - target[-1], 0.5)  # holds above 12.5 kHz, no spring back
        ripple = max(abs(response(f, gains) + gain_db - interp(f, ISO_F, target)) for f in GRID if 200 <= f <= 8000)
        self.assertLess(ripple, 0.5)  # follows the line between the ISO points, not only the points

    def test_strength(self):
        full, half = target_gains(-20, 40, 1.0), target_gains(-20, 40, 0.5)
        self.assertTrue(all(h >= f for h, f in zip(half, full)))  # weaker cut
        self.assertTrue(all(abs(g) < 1e-9 for g in target_gains(-20, 40, 0.0)))

    def test_biquad_models(self):
        self.assertAlmostEqual(biquad_db(20, "lowshelf", 200, 0.707, 6), 6, delta=0.1)
        self.assertAlmostEqual(biquad_db(15000, "lowshelf", 200, 0.707, 6), 0, delta=0.1)
        self.assertAlmostEqual(biquad_db(15000, "highshelf", 2000, 0.707, -4), -4, delta=0.2)
        self.assertAlmostEqual(biquad_db(1000, "highpass", 1000, 0.707), -3.01, delta=0.05)
        self.assertAlmostEqual(biquad_db(1000, "lowpass", 1000, 0.707), -3.01, delta=0.05)
        self.assertAlmostEqual(biquad_db(500, "peaking", 500, 2, 5), 5, delta=0.01)

    def test_fast_path_matches(self):
        freqs = tuple(GRID[::7])
        for kind, f0, q, g in (("peaking", 42, 8, -13.5), ("lowshelf", 200, 0.707, 6), ("highshelf", 14000, 0.7, -8),
                               ("lowpass", 110, 0.7071, 0), ("highpass", 110, 0.7071, 0)):
            for f, x in zip(freqs, biquad_db_many(freqs, kind, f0, q, g)):
                self.assertAlmostEqual(x, biquad_db(f, kind, f0, q, g), places=6)
        eq = check_eq([{"type": "peaking", "freq": 120, "q": 8, "gain": -12, "channel": "left"}])
        gains, _ = eq_params(-50, CFG)
        for f, x in zip(freqs, response_many(freqs, gains, eq)):
            self.assertAlmostEqual(x, response(f, gains, eq), places=6)

    def test_user_eq_never_boosts(self):
        eq = check_eq([{"type": "peaking", "freq": 356, "q": 0.49, "gain": 2.41},
                       {"type": "lowshelf", "freq": 80, "gain": 6},
                       {"type": "highpass", "freq": 30, "q": 0.5}])
        for knob in (0, -30):
            gains, mult = eq_params(knob, dict(CFG, ref_knob_db=0, eq=eq))
            gain_db = 20 * math.log10(mult)
            self.assertLessEqual(max(response(f, gains, eq) for f in GRID) + gain_db, 1e-9)
            self.assertLess(gain_db, -1)  # the boosts are compensated by the output gain

    def test_check_eq(self):
        self.assertEqual(check_eq([{"type": "lowpass", "freq": 100}])[0],
                         {"type": "lowpass", "freq": 100.0, "q": 0.707, "gain": 0.0, "channel": "both"})
        self.assertEqual(check_eq([{"type": "peaking", "freq": 45, "gain": -9, "channel": "right"}])[0]["channel"],
                         "right")
        for bad in ([{"type": "bell", "freq": 100}], [{"type": "peaking"}],
                    [{"type": "peaking", "freq": 100, "q": 0}], [{"type": "highpass", "freq": 30, "gain": 3}],
                    [{"type": "peaking", "freq": 100, "gain": 30}], {"type": "peaking"},
                    [{"type": "peaking", "freq": 100, "channel": "L"}]):
            with mock.patch.object(auto_loudness.sys, "exit", side_effect=SystemExit) as ex:
                with self.assertRaises(SystemExit):
                    check_eq(bad)
                ex.assert_called_once()

    def test_channel_eq_and_headroom(self):
        eq = check_eq([{"type": "peaking", "freq": 120, "q": 8, "gain": -12},
                       {"type": "peaking", "freq": 45, "q": 8, "gain": -9, "channel": "right"},
                       {"type": "peaking", "freq": 8000, "q": 1, "gain": 3, "channel": "left"}])
        self.assertEqual([b["freq"] for b in channel_eq(eq, "left")], [120, 8000])
        self.assertEqual([b["freq"] for b in channel_eq(eq, "right")], [120, 45])
        gains, mult = eq_params(0, dict(CFG, ref_knob_db=0, eq=eq))
        gain_db = 20 * math.log10(mult)
        self.assertAlmostEqual(gain_db, -3, delta=0.05)  # the left-only boost sets the common output gain
        for ch in ("left", "right"):
            self.assertLessEqual(max(response(f, gains, channel_eq(eq, ch)) for f in GRID) + gain_db, 1e-9)

    def test_mono_bass_split(self):
        for f in (20, 50, 90, 91, 150, 1000, 15000):
            self.assertAlmostEqual(abs(mono_bass_h(f, 90, 1, 1)), 1.0, places=6)   # mono content: flat
            self.assertLessEqual(abs(mono_bass_h(f, 90, 1, 0)), 1.0 + 1e-9)       # one side only: no boost
            self.assertLessEqual(abs(mono_bass_h(f, 90, 1, -1)), 1.0 + 1e-9)      # opposite polarity
        self.assertAlmostEqual(abs(mono_bass_h(20, 90, 1, 0)), 0.5, delta=0.01)    # deep bass split to both
        self.assertAlmostEqual(abs(mono_bass_h(5000, 90, 1, 0)), 1.0, delta=0.01)  # untouched above

    def test_filter_graph_is_consistent(self):
        eq = check_eq([{"type": "peaking", "freq": 120, "q": 8, "gain": -12},
                       {"type": "peaking", "freq": 45, "q": 8, "gain": -9, "channel": "right"}])
        gains = [0.0] * len(auto_loudness.BANDS)
        for mono in (0.0, 90.0):
            conf = filter_chain_conf("sink", eq, gains, 0.5, mono)
            names = set(re.findall(r"name = (\w+) label", conf))
            ends = re.findall(r'output = "(\w+):Out" input = "(\w+):In(?: (\d))?"', conf)
            used = {a for a, _, _ in ends} | {b for _, b, _ in ends}
            self.assertEqual(used, names)  # every link joins defined nodes and every node is linked
            for n in ("inL", "inR", "mixL", "mixR"):
                self.assertIn(n, names)
            inputs = [(b, p or "") for _, b, p in ends]
            self.assertEqual(len(inputs), len(set(inputs)))  # no input port fed twice
            self.assertEqual(sum(n.startswith("uR") for n in names), 2)
            self.assertEqual(sum(n.startswith("uL") for n in names), 1)
            self.assertEqual("mono" in names, bool(mono))

    def test_mono_bass_config(self):
        import tempfile
        from pathlib import Path
        for value, ok in ((0, True), (90, True), (20, False), (400, False)):
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "config.toml"
                path.write_text(f"mono_bass_hz = {value}\n")
                with mock.patch.object(auto_loudness, "CONFIG", path), \
                        mock.patch.object(auto_loudness.sys, "exit", side_effect=SystemExit):
                    if ok:
                        self.assertEqual(auto_loudness.load_config()["mono_bass_hz"], value)
                    else:
                        with self.assertRaises(SystemExit):
                            auto_loudness.load_config()

    def test_tray_state_and_icon(self):
        import xml.etree.ElementTree as ET
        import auto_loudness_tray as tray
        sinks = {"auto_loudness", "alsa_output.usb-Focusrite"}
        self.assertEqual(tray.state("auto_loudness", sinks), "active")
        self.assertEqual(tray.state("alsa_output.usb-Focusrite", sinks), "inactive")  # 4i4 picked directly
        self.assertEqual(tray.state("alsa_output.usb-Focusrite", {"alsa_output.usb-Focusrite"}), "hidden")
        self.assertEqual(tray.state("", set()), "hidden")  # no sound server
        ET.fromstring(tray.icon_svg(0.35))  # well-formed


if __name__ == "__main__":
    unittest.main()
