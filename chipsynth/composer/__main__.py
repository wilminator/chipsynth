# Copyright (C) 2026 Michael Allen Wilmes
"""python -m chipsynth.composer [GAMEDIR] [SONG] [--nosound] [--reset]

The song composer.  GAMEDIR's music folder is loaded (its .csp packages and loose .mml files) and SONG, if given, opens:
a song id like forest:dawn, or a path to a .mml / .csp file.  With no GAMEDIR it opens a generated tune so there is
something to look at.  Drop a file on the window or press Ctrl+O to open another; F1 shows the keys.
"""
import sys
from pathlib import Path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    nosound = "--nosound" in argv
    reset = "--reset" in argv
    args = [a for a in argv if not a.startswith("--")]
    import pygame
    from chipsynth import mml, musicgen
    from chipsynth.packageset import PackageSet
    from chipsynth.song import Song
    from .app import Composer, NullOut
    from .prefs import Prefs

    root = Path(args[0]) if args and Path(args[0]).is_dir() else None
    if root is not None:
        args = args[1:]
        from .files import library, tune_dir
        lib = library(root)
        packages = PackageSet(lib)
        packages.add_folder(tune_dir(root))
    else:
        lib = mml.builtin_library()
        packages = PackageSet(lib)
    prefs = Prefs() if reset else Prefs.load()
    if sys.platform == "win32":
        try:                                    # sharp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    pygame.init()
    screen = pygame.display.set_mode((prefs.width, prefs.height), pygame.RESIZABLE)
    pygame.display.set_caption("chipsynth composer")
    out = NullOut()
    if not nosound:
        from chipsynth.output import MusicOut
        out = MusicOut()
    app = Composer(screen, prefs, packages, lib, out, root=root)
    app.frame()                                 # lay out once so the first song is fitted to the real window
    target = args[0] if args else prefs.last
    opened = False
    if target:
        if ":" in target and not Path(target).exists() and packages.has(target):
            opened = app.open_song_id(target)
        elif Path(target).is_file():
            opened = app.open_path(target)
        elif root is not None and (tune_dir(root) / (target + ".mml")).is_file():
            opened = app.open_path(tune_dir(root) / (target + ".mml"))
    if not opened:
        refs = packages.songs()
        if refs and not args:
            opened = app.open_song_id(refs[0].id)
    if not opened:
        song = Song.from_mml(musicgen.generate("town", 3, "calm"), lib)
        app.load_song(song, "Generated town tune (drop a file here to open yours)")
    app.run()
    pygame.quit()


if __name__ == "__main__":
    main()
