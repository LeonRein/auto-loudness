import math
import unittest

from unittest import mock

import auto_loudness
from auto_loudness import (DEFAULTS, GRID, ISO_F, biquad_db, check_eq, contour, eq_params, interp,
                           parse_amixer, response, target_gains)

AMIXER = """numid=6,iface=MIXER,name='Master HW Playback Volume'
  ; type=INTEGER,access=r----R--,values=1,min=0,max=127,step=1
  : values=27
  | dBminmax-min=-127.00dB,max=0.00dB
"""
CFG = dict(DEFAULTS, ref_knob_db=-20)


class Test(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_amixer(AMIXER), -100.0)

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
                         {"type": "lowpass", "freq": 100.0, "q": 0.707, "gain": 0.0})
        for bad in ([{"type": "bell", "freq": 100}], [{"type": "peaking"}],
                    [{"type": "peaking", "freq": 100, "q": 0}], [{"type": "highpass", "freq": 30, "gain": 3}],
                    [{"type": "peaking", "freq": 100, "gain": 30}], {"type": "peaking"}):
            with mock.patch.object(auto_loudness.sys, "exit", side_effect=SystemExit) as ex:
                with self.assertRaises(SystemExit):
                    check_eq(bad)
                ex.assert_called_once()


if __name__ == "__main__":
    unittest.main()
