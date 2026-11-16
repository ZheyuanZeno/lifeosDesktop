"""Optional Linux/X11 desktop timer using the local timer API."""
import datetime as dt
import json
import math
import sys
import threading
import urllib.request

import cairo
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib


def elapsed(item):
    value = item.get('elapsed_seconds', 0)
    if item['status'] == 'running':
        start = dt.datetime.fromisoformat(item['phase_started_at'])
        if start.tzinfo is None:
            start = start.replace(tzinfo=dt.timezone.utc)
        value += max(0, int((dt.datetime.now(dt.timezone.utc) - start).total_seconds()))
    return value if item.get('mode') == 'countup' else min(value, item['duration_minutes'] * 60)


class TimerWindow(Gtk.Window):
    def __init__(self, url):
        super().__init__(title='LifeOS 专注计时')
        self.url, self.item, self.polling = url, None, False
        self.hovered, self.busy = False, False
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(144, 144)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.stick()
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect('enter-notify-event', self.hover, True)
        self.connect('leave-notify-event', self.hover, False)
        self.connect('button-press-event', self.drag)
        self.connect('draw', self.draw_timer)
        self.connect('realize', self.shape)
        self.connect('destroy', Gtk.main_quit)
        self.move(40, 100)
        GLib.timeout_add(1000, self.tick)
        self.tick()

    def shape(self, *_):
        surface = cairo.ImageSurface(cairo.FORMAT_A1, 144, 144)
        ctx = cairo.Context(surface)
        ctx.arc(72, 72, 70, 0, math.tau)
        ctx.fill()
        region = Gdk.cairo_region_create_from_surface(surface)
        self.get_window().shape_combine_region(region, 0, 0)
        self.get_window().input_shape_combine_region(region, 0, 0)

    def drag(self, _, event):
        if event.button == 1:
            if self.hovered and self.item and 88 <= event.y <= 116:
                if 22 <= event.x <= 70:
                    self.control('resume' if self.item['status'] == 'paused' else 'pause')
                    return True
                if 74 <= event.x <= 122:
                    self.control('finish')
                    return True
            self.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)
        return True

    def hover(self, _, event, entered):
        self.hovered = entered
        self.queue_draw()
        return False

    def control(self, action):
        if self.busy or not self.item:
            return
        self.busy = True
        item_id = self.item['id']
        self.queue_draw()
        threading.Thread(target=self.send_control, args=(action, item_id), daemon=True).start()

    def send_control(self, action, item_id):
        error = None
        try:
            with urllib.request.urlopen(self.url + '/api/state', timeout=3) as response:
                doc = json.load(response)
            request = urllib.request.Request(self.url + '/api/pomodoro',
                data=json.dumps({'action': action, 'id': item_id, 'expected_revision': doc['updated_at']}).encode(),
                headers={'Content-Type': 'application/json', 'X-LifeOS-Token': doc['token']})
            with urllib.request.urlopen(request, timeout=3) as response:
                json.load(response)
        except Exception:
            error = '操作未成功，请重试或在番茄钟中操作'
        GLib.idle_add(self.control_done, error)

    def control_done(self, error):
        self.busy = False
        if error:
            self.set_tooltip_text(error)
        self.tick()
        return False

    def tick(self):
        if not self.polling and not self.busy:
            self.polling = True
            threading.Thread(target=self.fetch, daemon=True).start()
        self.queue_draw()
        return True

    def fetch(self):
        try:
            with urllib.request.urlopen(self.url + '/api/state', timeout=2) as response:
                doc = json.load(response)
            item = next((x for x in doc.get('pomodoros', []) if not x.get('deleted') and x['status'] in ('running', 'paused')), None)
        except Exception:
            item = None
        GLib.idle_add(self.update_item, item)

    def update_item(self, item):
        self.polling = False
        if self.busy:
            return False
        self.item = item
        if item:
            self.show_all()
            self.set_keep_above(True)
            self.set_tooltip_text(item.get('label') or '专注计时 · 拖动可移动')
        else:
            self.hovered = False
            self.hide()
        self.queue_draw()
        return False

    def draw_timer(self, _, ctx):
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)
        ctx.arc(72, 72, 70, 0, math.tau)
        ctx.set_source_rgba(.97, .97, .98, .97)
        ctx.fill()
        ctx.arc(72, 72, 65, 0, math.tau)
        ctx.set_source_rgb(.84, .85, .89)
        ctx.set_line_width(2)
        ctx.stroke()
        if not self.item:
            return False
        used = elapsed(self.item)
        seconds = used if self.item.get('mode') == 'countup' else max(0, self.item['duration_minutes'] * 60 - used)
        text = f'{seconds // 60:02}:{seconds % 60:02}'
        ctx.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        lines = [(text, 26 if len(text) < 7 else 21, 72 if self.hovered else 79)]
        if not self.hovered:
            lines.append(('PAUSED' if self.item['status'] == 'paused' else 'FOCUS', 10, 102))
        for value, size, y in lines:
            ctx.set_font_size(size)
            ext = ctx.text_extents(value)
            ctx.set_source_rgb(.24, .25, .31)
            ctx.move_to(72 - ext.width / 2 - ext.x_bearing, y)
            ctx.show_text(value)
        if self.hovered:
            for x, label in [(22, '继续' if self.item['status'] == 'paused' else '暂停'), (74, '结束')]:
                ctx.set_source_rgb(.89, .90, .94)
                ctx.rectangle(x, 88, 48, 28)
                ctx.fill()
                ctx.set_source_rgb(.55, .55, .58) if self.busy else ctx.set_source_rgb(.24, .25, .31)
                ctx.set_font_size(12)
                ext = ctx.text_extents(label)
                ctx.move_to(x + 24 - ext.width / 2 - ext.x_bearing, 107)
                ctx.show_text(label)
        return False


if __name__ == '__main__':
    window = TimerWindow(sys.argv[1])
    Gtk.main()
