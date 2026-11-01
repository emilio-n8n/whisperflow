"""Overlay d'annotation : un cadre + une étiquette, transparent aux clics, qui disparaît seul.

Usage interne : python -m whisperflow.overlay X Y W H "texte" SECONDES [ECRAN]
Nécessite python-gobject + gtk4-layer-shell, et LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so
(géré par screen.Annotator). Coordonnées logiques relatives à l'écran.
"""
import sys


def main(argv: list[str]) -> int:
    x, y, w, h = (int(v) for v in argv[:4])
    label = argv[4] if len(argv) > 4 else ""
    seconds = float(argv[5]) if len(argv) > 5 else 4.0
    monitor_name = argv[6] if len(argv) > 6 else ""

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gdk, GLib, Gtk  # noqa: E402
    from gi.repository import Gtk4LayerShell as LS  # noqa: E402
    import cairo  # noqa: E402

    pad = 6

    def draw(_area, cr, width, height):
        cr.set_source_rgba(1.0, 0.33, 0.2, 0.95)
        cr.set_line_width(4)
        cr.rectangle(x - pad, y - pad, w + 2 * pad, h + 2 * pad)
        cr.stroke()
        if label:
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(15)
            ty = y - pad - 10 if y > 40 else y + h + pad + 22
            ext = cr.text_extents(label)
            cr.set_source_rgba(0.1, 0.1, 0.1, 0.85)
            cr.rectangle(x - pad, ty - ext.height - 6, ext.width + 12, ext.height + 12)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 1)
            cr.move_to(x - pad + 6, ty)
            cr.show_text(label)

    def on_activate(app):
        win = Gtk.Window(application=app)
        LS.init_for_window(win)
        LS.set_layer(win, LS.Layer.OVERLAY)
        for edge in (LS.Edge.TOP, LS.Edge.BOTTOM, LS.Edge.LEFT, LS.Edge.RIGHT):
            LS.set_anchor(win, edge, True)
        LS.set_exclusive_zone(win, -1)
        LS.set_keyboard_mode(win, LS.KeyboardMode.NONE)
        if monitor_name:
            display = Gdk.Display.get_default()
            mons = display.get_monitors()
            for i in range(mons.get_n_items()):
                m = mons.get_item(i)
                if m.get_connector() == monitor_name:
                    LS.set_monitor(win, m)
        css = Gtk.CssProvider()
        css.load_from_data(b"window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 800)
        area = Gtk.DrawingArea()
        area.set_draw_func(draw)
        win.set_child(area)
        win.present()
        surface = win.get_surface()
        if surface is not None:
            surface.set_input_region(cairo.Region())   # clics traversants
        GLib.timeout_add(int(seconds * 1000), lambda: (app.quit(), False)[1])

    app = Gtk.Application(application_id="dev.whisperflow.overlay")
    app.connect("activate", on_activate)
    return app.run([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
