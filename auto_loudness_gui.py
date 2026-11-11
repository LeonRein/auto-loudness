"""auto-loudness gui: a window that shows the filter curve for the current knob position.

Separate from the service on purpose: it never touches audio, and it computes the curve
with the service's own functions from the config and the knob. Nothing runs while the
window is closed; while it is open it only wakes up on ALSA control events.
"""
import math
import os
import subprocess as sp

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

import auto_loudness as core  # noqa: E402

APP_ID = "io.github.leonrein.AutoLoudness"  # also the .desktop name; a tray can activate this ID
DB_TOP, DB_BOTTOM = 3.0, -30.0
FREQS = core.GRID[::3]
X_TICKS = (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000)
DASHES = ([], [7, 5])  # left solid, right dashed


def curve(knob, cfg):
    """Total response in dB at FREQS per speaker, exactly what the service applies at this knob
    position: [(label, points)], one entry when both speakers get the same EQ."""
    gains, mult = core.eq_params(knob, cfg)
    db = 20 * math.log10(mult)
    curves = [(name, [x + db for x in core.response_many(FREQS, gains, core.channel_eq(cfg["eq"], name))])
              for _, name in core.CHANNELS]
    return curves if curves[0][1] != curves[1][1] else [("", curves[0][1])]


def draw_curve(cr, w, h, curves, fg, accent, message=None):
    """Draw grid, labels and the curves on a cairo context; colours are (r, g, b) tuples.
    curves: [(label, points)]; with two (left, right speaker) the second is dashed, with a legend."""
    left, right, top, bottom = 44, 12, 12, 26
    pw, ph = w - left - right, h - top - bottom
    x = lambda f: left + pw * math.log(f / 20) / math.log(1000)  # noqa: E731
    y = lambda db: top + ph * (DB_TOP - db) / (DB_TOP - DB_BOTTOM)  # noqa: E731
    cr.set_font_size(11)
    cr.set_line_width(1)
    for f in X_TICKS:
        cr.set_source_rgba(*fg, 0.15)
        cr.move_to(x(f), top)
        cr.line_to(x(f), top + ph)
        cr.stroke()
        label = f"{f // 1000}k" if f >= 1000 else str(f)
        cr.set_source_rgba(*fg, 0.6)
        cr.move_to(x(f) - cr.text_extents(label).width / 2, h - 8)
        cr.show_text(label)
    for db in range(0, int(DB_BOTTOM) - 1, -5):
        cr.set_source_rgba(*fg, 0.35 if db == 0 else 0.15)
        cr.move_to(left, y(db))
        cr.line_to(left + pw, y(db))
        cr.stroke()
        label = f"{db} dB"
        cr.set_source_rgba(*fg, 0.6)
        cr.move_to(left - 6 - cr.text_extents(label).width, y(db) + 4)
        cr.show_text(label)
    if message:
        cr.set_source_rgba(*fg, 0.8)
        cr.set_font_size(14)
        cr.move_to(left + (pw - cr.text_extents(message).width) / 2, top + ph / 2)
        cr.show_text(message)
        return
    cr.save()
    cr.rectangle(left, top, pw, ph)
    cr.clip()
    cr.set_source_rgb(*accent)
    cr.set_line_width(2.5)
    for n, (label, points) in enumerate(curves):
        cr.set_dash(DASHES[n])
        for i, (f, db) in enumerate(zip(FREQS, points)):
            (cr.line_to if i else cr.move_to)(x(f), y(db))
        cr.stroke()
    cr.restore()
    if len(curves) > 1:  # legend, bottom right, where the curves rarely are
        cr.set_font_size(12)
        for n, (label, _) in enumerate(curves):
            ly = top + ph - 12 - 18 * (len(curves) - 1 - n)
            lx = left + pw - 120
            cr.set_source_rgb(*accent)
            cr.set_line_width(2.5)
            cr.set_dash(DASHES[n])
            cr.move_to(lx, ly - 4)
            cr.line_to(lx + 34, ly - 4)
            cr.stroke()
            cr.set_dash([])
            cr.set_source_rgba(*fg, 0.85)
            cr.move_to(lx + 42, ly)
            cr.show_text(f"{label} speaker")


class CurveWindow(Adw.ApplicationWindow):
    """The curve plus the knob value; follows the knob while open."""

    def __init__(self, app):
        super().__init__(application=app, title="Loudness", default_width=640, default_height=360)
        self.cfg = core.load_config()
        self.points = self.message = self.monitor = self.watch = None
        self.refresh_pending = False
        header = Adw.HeaderBar()
        self.area = Gtk.DrawingArea(vexpand=True, hexpand=True)
        self.area.set_draw_func(self.on_draw)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(self.area)
        self.set_content(box)
        self.connect("close-request", lambda *_: self.stop_monitor())
        self.connect_card()

    def connect_card(self):
        """Find the 4i4 and start listening; retries every 2 s while it is missing."""
        card = core.find_card(self.cfg["card"])
        if card is None or not self.update(card):
            self.show_message(f"{self.cfg['card']} not connected")
            GLib.timeout_add_seconds(2, lambda: self.connect_card() if self.get_visible() else None)
            return
        # alsactl prints a line per control change; we read the knob ourselves on each one
        self.monitor = sp.Popen(["alsactl", "monitor", f"hw:{card}"], stdout=sp.PIPE, stderr=sp.DEVNULL)
        self.watch = GLib.io_add_watch(self.monitor.stdout.fileno(), GLib.PRIORITY_DEFAULT,
                                       GLib.IOCondition.IN | GLib.IOCondition.HUP, self.on_event, card)

    def on_event(self, fd, cond, card):
        if not os.read(fd, 4096):  # EOF: interface unplugged
            self.stop_monitor()
            self.connect_card()
            return False
        # a turn of the knob fires a burst of events: redraw once, for where the knob is now,
        # when the main loop is idle, instead of once per event
        if not self.refresh_pending:
            self.refresh_pending = True
            GLib.idle_add(self.on_refresh, card)
        return True

    def on_refresh(self, card):
        self.refresh_pending = False
        if self.monitor and not self.update(card):
            self.stop_monitor()
            self.connect_card()
        return False

    def update(self, card):
        knob = core.read_knob(card, self.cfg["control"])
        if knob is None:
            return False
        if self.points is None or knob != self.knob:
            self.knob, self.points, self.message = knob, curve(knob, self.cfg), None
            self.set_title(f"Loudness — knob {knob:+.1f} dB")
            self.area.queue_draw()
        return True

    def show_message(self, text):
        self.points, self.message = None, text
        self.set_title("Loudness")
        self.area.queue_draw()

    def stop_monitor(self):
        if self.watch:
            GLib.source_remove(self.watch)
        if self.monitor:
            self.monitor.terminate()
            self.monitor.wait()
        self.monitor = self.watch = None
        return False

    def on_draw(self, area, cr, w, h):
        fg = area.get_color()
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        draw_curve(cr, w, h, self.points, (fg.red, fg.green, fg.blue),
                   (accent.red, accent.green, accent.blue), self.message)


def main():
    app = Adw.Application(application_id=APP_ID)
    # single instance: launching again (menu, later a tray icon) just raises the open window
    app.connect("activate", lambda a: (a.get_active_window() or CurveWindow(a)).present())
    return app.run([])
