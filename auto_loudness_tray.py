"""auto-loudness tray: a panel icon that shows whether audio goes through the Loudness sink.

A StatusNotifierItem (as shown by GNOME's AppIndicator extension) spoken over plain D-Bus with
Gio, no GTK, so it idles at zero CPU: it only wakes on PulseAudio server/sink events. Icon:
bright when Loudness is the default output, dimmed when audio bypasses it (another output
picked), hidden when there is no Loudness sink (4i4 unplugged, service stopped). Its menu,
or a double click, opens the curve window (auto-loudness gui).
"""
import math
import os
import subprocess as sp
import sys
from pathlib import Path

from gi.repository import Gio, GLib

import auto_loudness as core

ITEM_PATH, MENU_PATH = "/StatusNotifierItem", "/MenuBar"
ICON = {"active": ("auto-loudness-active-symbolic", 1.0), "inactive": ("auto-loudness-inactive-symbolic", 0.35)}
STATUS_TEXT = {"active": "Active: audio goes through Loudness",
               "inactive": "Inactive: another output is the default", "hidden": ""}
SHOW_CURVE = 2  # menu item id; 1 is the status line

XML = """<node>
<interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/><property name="IconThemePath" type="s" access="read"/>
  <property name="Menu" type="o" access="read"/><property name="ItemIsMenu" type="b" access="read"/>
  <property name="IconName" type="s" access="read"/><property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionMovieName" type="s" access="read"/>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="ProvideXdgActivationToken"><arg name="token" type="s" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewIcon"/><signal name="NewTitle"/><signal name="NewStatus"><arg name="status" type="s"/></signal>
</interface>
<interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/><property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
    <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
  <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
    <arg type="a(ia{sv})" direction="out"/></method>
  <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/>
    <arg type="u" direction="in"/></method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/>
    <arg type="ai" direction="out"/></method>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface>
</node>"""


def state(default, sinks):
    """'hidden' without the Loudness sink, 'active' when it is the default output, else 'inactive'."""
    if core.SINK_NAME not in sinks:
        return "hidden"
    return "active" if default == core.SINK_NAME else "inactive"


def icon_svg(opacity):
    """16 px symbolic icon: the loudness 'bathtub' curve as a filled band (symbolic icons are
    recoloured by filling every path, so a plain stroke would turn into a filled blob)."""
    def bezier(t):  # (1.5, 2.5) -> (14.5, 5), dipping down like the compensation curve
        p = [(1.5, 2.5), (4.5, 13.5), (11, 12.5), (14.5, 5)]
        u = 1 - t
        return [u ** 3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t ** 3 * d for a, b, c, d in zip(*p)]
    upper, lower = [], []
    for i in range(33):
        t = i / 32
        (x0, y0), (x1, y1) = bezier(max(0, t - 0.01)), bezier(min(1, t + 0.01))
        n = math.hypot(x1 - x0, y1 - y0)
        nx, ny = -(y1 - y0) / n, (x1 - x0) / n
        x, y = bezier(t)
        upper.append(f"{x + nx:.2f},{y + ny:.2f}")
        lower.append(f"{x - nx:.2f},{y - ny:.2f}")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">'
            f'<path d="M{" L".join(upper + lower[::-1])} Z" fill="#bebebe" opacity="{opacity}"/></svg>')


def query():
    """(default sink name, set of sink names) from the PulseAudio server; empty when it is down."""
    try:
        default = sp.check_output(["pactl", "get-default-sink"], text=True, stderr=sp.DEVNULL).strip()
        sinks = {line.split("\t")[1] for line in sp.check_output(
            ["pactl", "list", "short", "sinks"], text=True, stderr=sp.DEVNULL).splitlines() if "\t" in line}
    except (sp.CalledProcessError, FileNotFoundError):
        return "", set()
    return default, sinks


class Tray:
    def __init__(self):
        self.icon_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "auto-loudness-icons"
        self.icon_dir.mkdir(exist_ok=True)
        for name, opacity in ICON.values():
            (self.icon_dir / f"{name}.svg").write_text(icon_svg(opacity))
        self.state, self.token, self.revision = "hidden", None, 1
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        self.bus.register_object(ITEM_PATH, node.interfaces[0], self.on_item_call, self.on_item_prop)
        self.bus.register_object(MENU_PATH, node.interfaces[1], self.on_menu_call, self.on_menu_prop)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        # (re-)register whenever the panel's watcher appears, e.g. after a GNOME Shell restart
        Gio.bus_watch_name_on_connection(self.bus, "org.kde.StatusNotifierWatcher", Gio.BusNameWatcherFlags.NONE,
                                         self.on_watcher, None)
        self.subscribe()

    def on_watcher(self, bus, name, owner):
        bus.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                 "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.name,)), None,
                 Gio.DBusCallFlags.NONE, -1, None, None)

    def subscribe(self):
        """Follow default-output and sink changes; restarts with the PulseAudio server."""
        self.refresh()
        self.events = sp.Popen(["pactl", "subscribe"], stdout=sp.PIPE, stderr=sp.DEVNULL)
        GLib.io_add_watch(self.events.stdout.fileno(), GLib.PRIORITY_DEFAULT,
                          GLib.IOCondition.IN | GLib.IOCondition.HUP, self.on_events)

    def on_events(self, fd, cond):
        data = os.read(fd, 4096)
        if not data:  # server gone (restart); try again shortly
            self.events.wait()
            GLib.timeout_add_seconds(2, lambda: self.subscribe() and False)
            return False
        if b"on server" in data or b"'new' on sink" in data or b"'remove' on sink" in data:
            self.refresh()
        return True

    def refresh(self):
        new = state(*query())
        if new == self.state:
            return
        self.state = new
        self.bus.emit_signal(None, ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon", None)
        self.bus.emit_signal(None, ITEM_PATH, "org.kde.StatusNotifierItem", "NewStatus",
                             GLib.Variant("(s)", (self.status(),)))
        self.revision += 1
        self.bus.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                             GLib.Variant("(ui)", (self.revision, 0)))

    def status(self):
        return "Passive" if self.state == "hidden" else "Active"

    def open_gui(self):
        env = dict(os.environ)
        if self.token:  # lets GNOME focus the window instead of showing "Loudness is ready"
            env["XDG_ACTIVATION_TOKEN"], self.token = self.token, None
        p = sp.Popen([sys.executable, os.path.abspath(sys.argv[0]), "gui"], env=env, start_new_session=True)
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, p.pid, lambda *_: None)  # reap it

    def on_item_prop(self, conn, sender, path, iface, prop):
        name, _ = ICON.get(self.state, ICON["inactive"])
        values = {"Category": ("s", "Hardware"), "Id": ("s", "auto-loudness"), "Title": ("s", "Loudness"),
                  "Status": ("s", self.status()), "WindowId": ("i", 0), "IconThemePath": ("s", str(self.icon_dir)),
                  "Menu": ("o", MENU_PATH), "ItemIsMenu": ("b", False), "IconName": ("s", name),
                  "OverlayIconName": ("s", ""), "AttentionIconName": ("s", ""), "AttentionMovieName": ("s", ""),
                  "IconPixmap": ("a(iiay)", []), "OverlayIconPixmap": ("a(iiay)", []),
                  "AttentionIconPixmap": ("a(iiay)", [])}
        return GLib.Variant(*values[prop])

    def on_item_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "ProvideXdgActivationToken":
            self.token = params.unpack()[0]
        elif method in ("Activate", "SecondaryActivate"):  # double / middle click
            self.open_gui()
        invocation.return_value(None)

    def items(self):
        return {1: {"label": GLib.Variant("s", STATUS_TEXT[self.state]), "enabled": GLib.Variant("b", False)},
                SHOW_CURVE: {"label": GLib.Variant("s", "Show filter curve")}}

    def on_menu_prop(self, conn, sender, path, iface, prop):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}[prop]

    def on_menu_call(self, conn, sender, path, iface, method, params, invocation):
        items = self.items()
        if method == "GetLayout":
            children = [GLib.Variant("(ia{sv}av)", (i, p, [])) for i, p in items.items()]
            root = (0, {"children-display": GLib.Variant("s", "submenu")}, children)
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, root)))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0] or list(items)
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i, items[i]) for i in ids if i in items],)))
        elif method == "GetProperty":
            i, name = params.unpack()
            invocation.return_value(GLib.Variant("(v)", (items.get(i, {}).get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            i, event, _, _ = params.unpack()
            if i == SHOW_CURVE and event == "clicked":
                self.open_gui()
            invocation.return_value(None)
        elif method == "EventGroup":
            for i, event, _, _ in params.unpack()[0]:
                if i == SHOW_CURVE and event == "clicked":
                    self.open_gui()
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))


def main():
    tray = Tray()  # noqa: F841 (kept alive by the main loop's callbacks)
    GLib.MainLoop().run()
