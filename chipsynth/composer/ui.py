# Copyright (C) 2026 Michael Allen Wilmes
# SPDX-License-Identifier: GPL-3.0-or-later
"""Themes, fonts and the small immediate-mode widget kit the composer draws with."""
import pygame

DARK = dict(
    bg=(24, 26, 31), panel=(33, 36, 44), panel2=(42, 46, 56), line=(62, 67, 80), text=(224, 228, 236),
    dim=(142, 149, 165), accent=(94, 170, 255), accent_text=(10, 20, 36), grid=(44, 48, 58), bar=(76, 84, 102),
    white_row=(40, 44, 54), black_row=(31, 34, 42), key_white=(214, 218, 226), key_black=(60, 64, 76),
    hot=(58, 64, 78), good=(96, 214, 142), warn=(255, 196, 92), loop=(46, 40, 62), past=(20, 22, 27),
    voices=[(255, 122, 112), (245, 197, 66), (64, 208, 176), (181, 140, 255), (94, 170, 255), (255, 143, 200)])
LIGHT = dict(
    bg=(238, 240, 244), panel=(250, 251, 253), panel2=(232, 235, 241), line=(196, 202, 214), text=(32, 36, 46),
    dim=(105, 113, 130), accent=(34, 118, 224), accent_text=(255, 255, 255), grid=(224, 228, 236), bar=(170, 178, 194),
    white_row=(255, 255, 255), black_row=(240, 242, 247), key_white=(255, 255, 255), key_black=(70, 76, 90),
    hot=(226, 232, 244), good=(28, 150, 84), warn=(190, 120, 10), loop=(240, 234, 252), past=(222, 225, 232),
    voices=[(216, 72, 62), (200, 150, 10), (14, 156, 128), (124, 82, 214), (34, 118, 224), (214, 80, 150)])

SANS = "segoeui,helveticaneue,dejavusans,arial,freesans,liberationsans"
MONO = "consolas,dejavusansmono,couriernew,liberationmono,freemono"


def mix(a, b, t):
    """Colour a blended toward b by t (0 = a, 1 = b)."""
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


class Fonts:
    def __init__(self, px):
        self.px = px
        self.ui = pygame.font.SysFont(SANS, px)
        self.small = pygame.font.SysFont(SANS, max(9, int(round(px * 0.82))))
        self.bold = pygame.font.SysFont(SANS, px, bold=True)
        self.mono = pygame.font.SysFont(MONO, max(9, int(round(px * 0.9))))
        self.line = self.ui.get_linesize()


_cache = {}


def render(font, s, color):
    key = (id(font), s, color)
    surf = _cache.get(key)
    if surf is None:
        if len(_cache) > 2000:
            _cache.clear()
        surf = _cache[key] = font.render(s, True, color)
    return surf


def fit(font, s, max_w):
    """`s` shortened with an ellipsis to fit max_w pixels."""
    if max_w <= 0:
        return ""
    if font.size(s)[0] <= max_w:
        return s
    while s and font.size(s + "…")[0] > max_w:
        s = s[:-1]
    return s + "…" if s else ""


def text(surf, font, s, pos, color, anchor="tl", max_w=None):
    """Draw `s`; anchor is two letters of t/m/b and l/c/r.  Returns the rect drawn."""
    if max_w is not None:
        s = fit(font, s, max_w)
    img = render(font, s, color)
    r = img.get_rect()
    v, h = anchor[0], anchor[1]
    x, y = pos
    r.x = x if h == "l" else x - r.w // 2 if h == "c" else x - r.w
    r.y = y if v == "t" else y - r.h // 2 if v == "m" else y - r.h
    surf.blit(img, r)
    return r


def rrect(surf, color, rect, radius=4, border=None, width=1):
    rect = pygame.Rect(rect)
    if rect.w <= 0 or rect.h <= 0:
        return
    pygame.draw.rect(surf, color, rect, border_radius=radius)
    if border:
        pygame.draw.rect(surf, border, rect, width, border_radius=radius)


class Ui:
    """Immediate-mode helpers.  The app feeds it mouse events; a widget call draws and reports a click."""

    def __init__(self):
        self.pos = (-1, -1)
        self.press = None                # where the left button went down
        self.click = None                # where it came back up (for one frame)
        self.tip = ""                    # the tooltip of whatever the mouse is over this frame
        self.dd_open = None              # key of the drop-down whose list is showing, or None
        self.dd_hi = 0                   # the highlighted row of that list
        self._dd = {}                    # key -> (box rect, options, index) of the latest frame's drop-downs
        self._dd_new = False             # the list was opened by this frame's click

    def begin(self):
        self.tip = ""
        self._dd = {}
        self._dd_new = False

    def end(self):
        self.click = None

    def hover(self, rect):
        return pygame.Rect(rect).collidepoint(self.pos)

    def clicked(self, rect):
        r = pygame.Rect(rect)
        return self.click is not None and r.collidepoint(self.click) and self.press is not None and r.collidepoint(self.press)

    def button(self, surf, th, fonts, rect, label, tip="", on=False, enabled=True, icon=None, font=None):
        rect = pygame.Rect(rect)
        hot = enabled and self.hover(rect)
        if hot and tip:
            self.tip = tip
        if on:
            bg, fg, border = th["accent"], th["accent_text"], th["accent"]
        elif not enabled:
            bg, fg, border = th["panel"], mix(th["dim"], th["panel"], 0.4), th["line"]
        else:
            bg, fg, border = (th["hot"] if hot else th["panel2"]), th["text"], th["line"]
        rrect(surf, bg, rect, 5, border)
        cx, cy = rect.center
        if icon == "play":
            s = rect.h * 0.28
            pygame.draw.polygon(surf, fg, [(cx - s * 0.7, cy - s), (cx - s * 0.7, cy + s), (cx + s, cy)])
        elif icon == "stop":
            s = rect.h * 0.24
            pygame.draw.rect(surf, fg, (cx - s, cy - s, 2 * s, 2 * s), border_radius=2)
        else:
            text(surf, font or fonts.ui, label, (cx, cy), fg, "mc", rect.w - 6)
        return enabled and self.clicked(rect)

    # ---- drop-down: dropdown() draws the closed box, finish_dropdowns() (call it last, after every other
    # widget of the dialog) draws the open list on top and reports a pick.
    def dropdown(self, surf, th, fonts, rect, key, options, index, focus=False, tip=""):
        rect = pygame.Rect(rect)
        hot = self.hover(rect) and self.dd_open is None
        if hot and tip:
            self.tip = tip
        rrect(surf, th["hot"] if hot else th["panel2"], rect, 5, th["accent"] if (focus or self.dd_open == key) else th["line"])
        text(surf, fonts.ui, options[index], (rect.x + 8, rect.centery), th["text"], "ml", rect.w - 30)
        cx, cy, a = rect.right - 12, rect.centery, max(3, rect.h // 7)
        pygame.draw.polygon(surf, th["dim"], [(cx - a, cy - a // 2), (cx + a, cy - a // 2), (cx, cy + a // 2 + 1)])
        self._dd[key] = (rect, list(options), index)
        if self.dd_open is None and self.clicked(rect):
            self.open_dropdown(key, index)
            return True
        return False

    def open_dropdown(self, key, index):
        self.dd_open, self.dd_hi = key, index
        self._dd_new = True

    def dropdown_key(self, k):
        """Keys while a list is open.  Returns ('pick', key, i), ('close',), ('none',), or None if no list is open."""
        if self.dd_open is None or self.dd_open not in self._dd:
            return None
        n = len(self._dd[self.dd_open][1])
        if k == pygame.K_UP:
            self.dd_hi = (self.dd_hi - 1) % n
        elif k == pygame.K_DOWN:
            self.dd_hi = (self.dd_hi + 1) % n
        elif k == pygame.K_HOME:
            self.dd_hi = 0
        elif k == pygame.K_END:
            self.dd_hi = n - 1
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            key, self.dd_open = self.dd_open, None
            return ("pick", key, self.dd_hi)
        elif k == pygame.K_ESCAPE:
            self.dd_open = None
            return ("close",)
        return ("none",)

    @staticmethod
    def list_rect(box, n, fonts, screen_h):
        """(row height, rect) of the open list for a drop-down box: below it, or above if there is no room."""
        rh = int(fonts.line * 1.3)
        lst = pygame.Rect(box.x, box.bottom + 2, box.w, rh * n + 4)
        if lst.bottom > screen_h - 4:
            lst.y = box.y - 2 - lst.h
        return rh, lst

    def row_rect(self, key, i, fonts, screen_h):
        """Where row i of the open (or just-drawn) list for `key` is."""
        box, options, _ = self._dd[key]
        rh, lst = self.list_rect(box, len(options), fonts, screen_h)
        return pygame.Rect(lst.x + 2, lst.y + 2 + i * rh, lst.w - 4, rh)

    def finish_dropdowns(self, surf, th, fonts):
        """Draw the open list (if any) over everything; a click on a row picks it, a click anywhere else closes it.
        Returns (key, index) when something was picked this frame, else None."""
        key = self.dd_open
        if key is None:
            return None
        if key not in self._dd:
            self.dd_open = None
            return None
        picked = None
        box, options, index = self._dd[key]
        rh, lst = self.list_rect(box, len(options), fonts, surf.get_height())
        keep = surf.get_clip()
        surf.set_clip(None)
        rrect(surf, th["panel"], lst, 5, th["accent"])
        for i, o in enumerate(options):
            row = pygame.Rect(lst.x + 2, lst.y + 2 + i * rh, lst.w - 4, rh)
            if self.hover(row):
                self.dd_hi = i
            if i == self.dd_hi:
                rrect(surf, th["accent"], row, 4)
            text(surf, fonts.ui, o, (row.x + 8, row.centery), th["accent_text"] if i == self.dd_hi else
                 (th["accent"] if i == index else th["text"]), "ml", row.w - 12)
            if self.click is not None and self.clicked(row):
                picked = (key, i)
        surf.set_clip(keep)
        if picked:
            self.dd_open = None
        elif self.click is not None and not self._dd_new and not self.clicked(lst):
            self.dd_open = None
        return picked


def label_width(fonts, label, pad=26, font=None):
    return (font or fonts.ui).size(label)[0] + pad
