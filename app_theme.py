"""Compact white / charcoal / seafoam styling matching the user's TSDP app."""
from tkinter import ttk

BG = '#ffffff'
FG = '#3b413c'
DIM = '#68716b'
SOFT = '#daf0ee'
ACCENT = '#94d1be'


def apply_theme(root):
    s = ttk.Style(root)
    s.theme_use('clam')
    s.configure('.', background=BG, foreground=FG, fieldbackground=BG,
                bordercolor=SOFT, troughcolor=SOFT, focuscolor=ACCENT,
                selectbackground=ACCENT, selectforeground=FG, font=('Segoe UI', 9))
    s.configure('TFrame', background=BG)
    s.configure('TLabel', background=BG, foreground=FG)
    s.configure('Title.TLabel', font=('Segoe UI Semibold', 15))
    s.configure('Heading.TLabel', font=('Segoe UI Semibold', 12))
    s.configure('Muted.TLabel', foreground=DIM)
    s.configure('TButton', background=SOFT, foreground=FG, bordercolor=SOFT,
                padding=(10, 6), relief='flat', borderwidth=0)
    s.map('TButton', background=[('disabled', BG), ('pressed', ACCENT), ('active', ACCENT)],
          foreground=[('disabled', DIM)])
    s.configure('Accent.TButton', background=ACCENT, bordercolor=ACCENT)
    s.configure('TEntry', padding=4, insertcolor=FG)
    s.configure('TCheckbutton', background=BG, foreground=FG)
    s.map('TCheckbutton', background=[('active', BG)])
    s.configure('TNotebook', background=BG, borderwidth=0, tabmargins=(12, 8, 12, 0))
    s.configure('TNotebook.Tab', background=BG, foreground=DIM, padding=(18, 9))
    s.map('TNotebook.Tab', background=[('selected', SOFT), ('active', SOFT)],
          foreground=[('selected', FG)], expand=[('selected', (0, 0, 0, 1))])
    s.configure('Treeview', background=BG, fieldbackground=BG, foreground=FG,
                bordercolor=SOFT, rowheight=23)
    s.configure('Treeview.Heading', background=SOFT, foreground=FG,
                font=('Segoe UI Semibold', 9), relief='flat')
    s.map('Treeview', background=[('selected', ACCENT)], foreground=[('selected', FG)])
    s.configure('Horizontal.TProgressbar', troughcolor=SOFT, background=ACCENT,
                bordercolor=SOFT, lightcolor=ACCENT, darkcolor=ACCENT, thickness=7)
    s.configure('Statusbar.TFrame', background=SOFT)
    s.configure('Statusbar.TLabel', background=SOFT, foreground=DIM, font=('Segoe UI', 8))
    s.configure('TLabelframe', background=BG, bordercolor=SOFT, relief='flat', borderwidth=1)
    s.configure('TLabelframe.Label', font=('Segoe UI Semibold', 9))
    s.configure('Vertical.TScrollbar', background=BG, troughcolor=BG, bordercolor=BG, arrowcolor=DIM)
