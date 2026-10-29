"""What a conversion changed.  Never silent: every dropped or approximated thing is a line here."""
from dataclasses import dataclass, field


@dataclass
class Report:
    notes: list = field(default_factory=list)          # facts (the file's version, how many songs)
    approximated: list = field(default_factory=list)   # carried over, but not exactly
    dropped: list = field(default_factory=list)        # left out

    def note(self, msg):
        if msg not in self.notes:
            self.notes.append(msg)

    def approx(self, msg):
        if msg not in self.approximated:
            self.approximated.append(msg)

    def drop(self, msg):
        if msg not in self.dropped:
            self.dropped.append(msg)

    @property
    def clean(self):
        return not self.approximated and not self.dropped

    def lines(self):
        out = ["note: " + m for m in self.notes]
        out += ["approximated: " + m for m in self.approximated]
        out += ["left out: " + m for m in self.dropped]
        return out

    def as_dict(self):
        return {"notes": list(self.notes), "approximated": list(self.approximated), "dropped": list(self.dropped)}
