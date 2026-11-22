#!/usr/bin/env bash
# Install a built wheel into a fresh virtual environment (core only: no pygame) and use it the way a user would.
#   .github/scripts/smoke-wheel.sh dist/chipsynth-X.Y.Z-py3-none-any.whl [python]
# Used by .github/workflows/build.yml; run it by hand the same way before a release if you like.
set -euo pipefail

wheel=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
python=${2:-python}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

"$python" -m venv "$work/venv"
if [ -d "$work/venv/Scripts" ]; then bin="$work/venv/Scripts"; else bin="$work/venv/bin"; fi

"$bin/python" -m pip install --quiet "$wheel"

# Run from an empty folder so the source tree cannot shadow the installed package.
cd "$work"

echo "--- the installed files"
"$bin/python" - <<'PY'
import chipsynth, pathlib
root = pathlib.Path(chipsynth.__file__).resolve().parent
assert "site-packages" in root.parts, "chipsynth was not imported from the installed wheel: %s" % root
assert not (root / "tests").exists(), "the tests must not be in the wheel"
import importlib.util
assert importlib.util.find_spec("pygame") is None, "the core install must not pull in pygame"
import chipsynth.dpcm, chipsynth.kit, chipsynth.midi, chipsynth.formats.goattracker   # nothing here may need pygame
print("ok:", root)
PY

echo "--- the commands"
"$bin/chipsynth" --help > /dev/null
"$bin/python" -m chipsynth --help > /dev/null
test -e "$bin/chipsynth-composer" || test -e "$bin/chipsynth-composer.exe"

echo "--- a tiny export"
printf 'title Tiny\ntempo 120\nV1: @lead l8 o4 c e g >c<\n' > tiny.mml
"$bin/chipsynth" export-song tiny.mml -o tiny.wav
"$bin/python" -m chipsynth export-song tiny.mml -o tiny.mid
"$bin/python" -m chipsynth generate town --seed 3 -o town.mml
test "$(wc -c < tiny.wav)" -gt 10000
test -s tiny.mid
test -s town.mml
"$bin/python" - <<'PY'
import wave
with wave.open("tiny.wav") as w:
    assert w.getnchannels() == 2 and w.getframerate() == 44100 and w.getnframes() > 44100, "unexpected WAV"
    print("ok: tiny.wav is stereo 44.1 kHz,", w.getnframes(), "frames")
PY
echo "wheel smoke test passed"
