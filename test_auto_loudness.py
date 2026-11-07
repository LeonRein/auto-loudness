import math
import unittest

from auto_loudness import (DEFAULTS, GRID, ISO_F, contour, eq_params, interp, parse_amixer,
                           response, target_gains)

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


if __name__ == "__main__":
    unittest.main()
