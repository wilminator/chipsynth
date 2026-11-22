"""Retired 2026-11-22: the FamiStudio Version stamp (a default, a typed version, a reference file).

The exporter now never writes a Version: FamiStudio's newer sources take a file without one as their own version, and the author's
upgraded FamiStudio opens such files (a release that demands one, like 4.2.0, is refused: the answer is to upgrade FamiStudio).
Kept for review, not imported by anything.  These were `reference_version` / `stamp_note` in formats/famistudio_export.py,
DEFAULT_VERSION = "4.5.3" there, and `fs_stamp` in exporter.py; the composer's Version row and its settings fs_stamp / fs_typed / fs_ref,
and the CLI's --fs-version / --fs-reference, drove them.
"""

DEFAULT_VERSION = "4.5.3"


def reference_version(text):
    """The exact Version string on the Project line (the first line) of a FamiStudio text file, or None when the line has
    none.  Raises FamiStudioError when the text is not a FamiStudio text file."""
    for line in text.lstrip("\ufeff").splitlines():
        if line.strip():
            got = split_line(line)
            if got and got[0] == "Project":
                return got[1].get("Version") or None
            break
    raise FamiStudioError("this is not a FamiStudio text file (its first line is not a Project line)")


def stamp_note(version):
    """The Report line that says which stamp was written and why another might be needed."""
    if version is None:
        return ("no Version attribute written: newer FamiStudio sources then skip their version check, but a release that still "
                "demands one (for example 4.2.0) refuses the file; if yours does, export again with a reference file or the version it names")
    return ('Version="%s" written. FamiStudio opens a text file only when the first two numbers of its Version match its own; '
            "if yours says the file is from an incompatible version, export again with a reference file from your FamiStudio, "
            "the version it shows typed in, or None" % version)


def fs_stamp(version=DEFAULT_VERSION, reference=None):
    """(the Version to write or None, a Report line about where it came from or None).  `version` may be the word "none";
    a `reference` file wins over `version`.  Raises ExportError when the reference cannot be read or is not FamiStudio text."""
    if reference:
        try:
            text = Path(reference).read_text(encoding="utf-8", errors="replace")
            got = famistudio_export.reference_version(text)
        except OSError as e:
            raise ExportError("can't read the reference file %s: %s" % (reference, e.strerror or e))
        except FamiStudioError as e:
            raise ExportError("%s: %s" % (reference, e))
        if got is None:
            return None, "the reference file %s has no Version attribute, so none is written" % Path(reference).name
        return got, 'Version "%s" copied from the reference file %s' % (got, Path(reference).name)
    if version is None or str(version).strip().lower() in ("none", ""):
        return None, None
    return str(version).strip(), None
