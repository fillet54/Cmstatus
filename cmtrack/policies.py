"""Release policies.

A policy turns a CI's release rules into planned releases and versions, names
spawned patch/emergency releases, and gates promotion. Policies are data
(a row in the ``policy`` table: type + JSON params) interpreted by a class
registered here, so new behaviour means a new class, not a schema change.

    class MyPolicy(Policy):
        type = "my-policy"
        defaults = {**Policy.defaults, "foo": 1}
        def plan(self, start, end): ...
    register(MyPolicy)
"""
import datetime as dt
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Type


class PolicyError(ValueError):
    pass


@dataclass
class PlannedVersion:
    name: str
    planned_date: Optional[str] = None


@dataclass
class PlannedRelease:
    name: str
    target_date: Optional[str] = None
    versions: List[PlannedVersion] = field(default_factory=list)


def add_months(d: dt.date, n: int, day: int = 1) -> dt.date:
    m = d.month - 1 + n
    return dt.date(d.year + m // 12, m % 12 + 1, day)


def parse_date(value, what="date") -> Optional[dt.date]:
    if value in (None, ""):
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise PolicyError(f"invalid {what} {value!r}; expected YYYY-MM-DD") from None


class Policy:
    """Base policy: plans nothing. Also used for CIs without a policy (e.g. placeholders)."""

    type = "none"
    defaults = {
        "version_format": "{release}-b{seq}",       # names for versions of planned releases
        "child_version_format": "{release}-r{seq}",  # extra versions (respins) of patch/emergency releases
        "patch_format": "{release}.P{n}",       # tokens: {release} root release, {base} base version, {n}
        "emergency_format": "{release}.ER{n}",
        "spawn_kinds": ["patch", "emergency"],
        "max_open": {"patch": 1, "emergency": 1},    # open children per kind per release line; null = unlimited
        "require_tested": False,                     # promotion gate: version must be 'tested'
    }

    def __init__(self, params: Optional[dict] = None, base_dir: Optional[str] = None):
        params = dict(params or {})
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise PolicyError(f"unknown {self.type} policy params: {sorted(unknown)}")
        self.params = {**self.defaults, **params}
        self.base_dir = base_dir
        self.validate()

    def __getitem__(self, key):
        return self.params[key]

    # -- hooks ---------------------------------------------------------------
    def validate(self) -> None:
        for key in ("version_format", "child_version_format"):
            self._check_format(key, release="X", seq=1)
        for key in ("patch_format", "emergency_format"):
            self._check_format(key, release="X", base="X", n=1)
        bad = set(self["spawn_kinds"]) - {"patch", "emergency"}
        if bad:
            raise PolicyError(f"spawn_kinds may only contain patch/emergency, got {sorted(bad)}")
        max_open = self["max_open"] or {}
        if not isinstance(max_open, dict) or any(
                k not in ("patch", "emergency") or not (v is None or (isinstance(v, int) and v >= 1))
                for k, v in max_open.items()):
            raise PolicyError("max_open must map patch/emergency to a positive int or null")

    def plan(self, start: dt.date, end: dt.date) -> List[PlannedRelease]:
        return []

    def version_name(self, release: dict, seq: int) -> str:
        key = "version_format" if release["kind"] == "planned" else "child_version_format"
        return self[key].format(release=release["name"], seq=seq)

    def child_release_name(self, kind: str, release_name: str, base_version_name: str, n: int) -> str:
        return self[f"{kind}_format"].format(release=release_name, base=base_version_name, n=n)

    def release_gate(self, version: dict) -> List[str]:
        """Return the reasons ``version`` may not be promoted (empty list = OK)."""
        allowed = ("tested",) if self["require_tested"] else ("built", "tested")
        if version["status"] not in allowed:
            return [f"version {version['name']} is {version['status']}; must be {' or '.join(allowed)}"]
        return []

    # -- helpers -------------------------------------------------------------
    def _check_format(self, key, **sample):
        try:
            self[key].format(**sample)
        except (KeyError, IndexError, ValueError) as e:
            raise PolicyError(f"bad {key} {self[key]!r}: {e}") from None


class CadencePolicy(Policy):
    """Fixed cadence, e.g. a release every quarter with a build every month.

    Each release period gets ``release_months // build_months`` planned versions;
    the last one is the intended release candidate. Tokens for ``release_format``:
    {year} {fy} {quarter} {month} ({fy} is the fiscal year, named by the year it ends,
    when anchor_month != 1; e.g. anchor_month=10 gives US Government FY).
    """

    type = "cadence"
    defaults = {
        **Policy.defaults,
        "release_months": 3,
        "build_months": 1,
        "anchor_month": 1,    # month a release period starts on (1 = calendar quarters, 10 = US FY)
        "build_day": 15,      # day of month for planned build dates
        "release_format": "{year}.Q{quarter}",
        "require_tested": True,
    }

    def validate(self):
        super().validate()
        r, b = self["release_months"], self["build_months"]
        if not (isinstance(r, int) and isinstance(b, int) and r > 0 and b > 0 and r % b == 0):
            raise PolicyError("release_months and build_months must be positive ints, release a multiple of build")
        if not 1 <= self["anchor_month"] <= 12:
            raise PolicyError("anchor_month must be 1..12")
        if not 1 <= self["build_day"] <= 28:
            raise PolicyError("build_day must be 1..28")
        self._check_format("release_format", year=2026, fy=2026, quarter=1, month=1)

    def _tokens(self, period_start: dt.date) -> dict:
        anchor = self["anchor_month"]
        fy = period_start.year + (1 if anchor != 1 and period_start.month >= anchor else 0)
        quarter = ((period_start.month - anchor) % 12) // self["release_months"] + 1
        return {"year": period_start.year, "fy": fy, "quarter": quarter, "month": period_start.month}

    def plan(self, start, end):
        step, build = self["release_months"], self["build_months"]
        # first period that overlaps `start`
        p = dt.date(start.year, self["anchor_month"], 1)
        while p > start:
            p = add_months(p, -step)
        while add_months(p, step) <= start:
            p = add_months(p, step)

        out = []
        while p < end:
            name = self["release_format"].format(**self._tokens(p))
            rel = PlannedRelease(name=name)
            for seq in range(1, step // build + 1):
                when = add_months(p, seq * build - 1, self["build_day"])
                rel.versions.append(PlannedVersion(
                    self.version_name({"kind": "planned", "name": name}, seq), when.isoformat()))
            rel.target_date = rel.versions[-1].planned_date
            out.append(rel)
            p = add_months(p, step)
        return out


class ManualPolicy(Policy):
    """Plan maintained by hand in a text file (``path``, relative to POLICY_DIR).

    Format (indentation optional, dates optional, '#' starts a comment):

        release 4.1.0 2027-03-31
            version 4.1.0-rc1 2027-02-15
            version 4.1.0     2027-03-15

    The whole file is synced each time; the planning window is ignored.
    Versions not listed keep the default naming when added ad hoc.
    """

    type = "manual"
    defaults = {**Policy.defaults, "path": None}

    def validate(self):
        super().validate()
        if not self["path"]:
            raise PolicyError("manual policy requires 'path'")

    @property
    def file_path(self) -> str:
        path = self["path"]
        if not os.path.isabs(path) and self.base_dir:
            path = os.path.join(self.base_dir, path)
        return path

    def plan(self, start, end):
        try:
            with open(self.file_path, encoding="utf-8") as f:
                return self.parse(f.read(), self.file_path)
        except FileNotFoundError:
            raise PolicyError(f"release plan file not found: {self.file_path}") from None

    @staticmethod
    def parse(text: str, source: str = "<text>") -> List[PlannedRelease]:
        releases: List[PlannedRelease] = []
        seen = set()
        for lineno, raw in enumerate(text.splitlines(), 1):
            words = raw.split("#", 1)[0].split()
            if not words:
                continue
            where = f"{source}:{lineno}"
            keyword, args = words[0].lower(), words[1:]
            if keyword not in ("release", "version") or not 1 <= len(args) <= 2:
                raise PolicyError(f"{where}: expected 'release NAME [DATE]' or 'version NAME [DATE]'")
            name = args[0]
            date = parse_date(args[1], f"date at {where}").isoformat() if len(args) == 2 else None
            if (keyword, name) in seen:
                raise PolicyError(f"{where}: duplicate {keyword} {name}")
            seen.add((keyword, name))
            if keyword == "release":
                releases.append(PlannedRelease(name, date))
            elif not releases:
                raise PolicyError(f"{where}: version before any release")
            else:
                releases[-1].versions.append(PlannedVersion(name, date))
        return releases


REGISTRY: Dict[str, Type[Policy]] = {}


def register(cls: Type[Policy]) -> Type[Policy]:
    REGISTRY[cls.type] = cls
    return cls


for _cls in (Policy, CadencePolicy, ManualPolicy):
    register(_cls)


def build(type_: str, params: Optional[dict] = None, base_dir: Optional[str] = None) -> Policy:
    try:
        cls = REGISTRY[type_]
    except KeyError:
        raise PolicyError(f"unknown policy type {type_!r}; known: {sorted(REGISTRY)}") from None
    return cls(params, base_dir)
