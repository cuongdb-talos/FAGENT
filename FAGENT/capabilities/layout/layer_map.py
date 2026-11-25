"""Normalize stream layer maps and retain the source of each entry."""

from pathlib import Path


class LayerMap:
    def run(self, paths: list[Path]) -> "LayerMap":
        self.entries = []
        self.by_pair = {}
        self.ambiguous = {}
        by_name = {}
        by_stream = {}
        for raw_path in paths:
            path = Path(raw_path)
            for line_no, line in enumerate(path.read_text().splitlines(), 1):
                fields = line.split("#", 1)[0].split("//", 1)[0].split()
                if not fields:
                    continue
                if len(fields) in (3, 4) and all(v.isdecimal() for v in fields[:2]):
                    layer, datatype = map(int, fields[:2])
                    name = fields[2]
                    purpose = fields[3] if len(fields) == 4 else "unknown"
                elif len(fields) == 4 and all(v.isdecimal() for v in fields[2:]):
                    name, purpose = fields[:2]
                    layer, datatype = map(int, fields[2:])
                else:
                    raise ValueError(f"{path}:{line_no}: invalid layer map entry: {line.strip()}")
                name, purpose = name.upper(), purpose.lower()
                entry = dict(name=name, purpose=purpose, layer=layer, datatype=datatype,
                             source=str(path), line=line_no)
                self.entries.append(entry)
                pair = layer, datatype
                by_name.setdefault((name, purpose), {}).setdefault(pair, []).append(entry)
                by_stream.setdefault((*pair, purpose), {}).setdefault(name, []).append(entry)
                self.by_pair.setdefault(pair, [name, purpose])
                if purpose == "drawing":
                    self.by_pair[pair] = [name, purpose]
        for groups in (by_name, by_stream):
            for key, choices in groups.items():
                if len(choices) > 1:
                    self.ambiguous[key] = [entry for bucket in choices.values() for entry in bucket]
        return self

    def require_unambiguous(self):
        if self.ambiguous:
            locations = [
                "{}:{} ({}/{} = {}/{})".format(
                    e["source"], e["line"], e["name"], e["purpose"], e["layer"], e["datatype"]
                )
                for entries in self.ambiguous.values() for e in entries
            ]
            raise ValueError("Conflicting layer mappings: " + ", ".join(dict.fromkeys(locations)))
