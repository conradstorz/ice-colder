# services/dispensers.py
"""Validation pipeline for the hand-edited `dispensers.toml` (plan:
dispenser profiles, Task 3). Each `[slot.N]` table describes one physical
dispense slot's mechanism; `validate_document` runs the whole §5.2
pipeline -- TOML syntax, top-level shape, per-slot schema
(`services/dispenser_schema.py`), then cross-checks against the product
catalog, channel roles, the dispense time budget and (optionally) the
vending board's declared capabilities -- and returns a `ValidationReport`
that never lets one bad slot sink the others.

Primitives: `validate_document`, `humanize`, `find_line`. Task 4 adds
`DispenserProfiles` (load/validate/save) on top of this; Task 6 adds the
`--check` CLI.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tomllib
import types
import typing
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from difflib import get_close_matches
from pathlib import Path
from typing import Literal

import annotated_types
from pydantic import BaseModel, TypeAdapter, ValidationError

from config.config_model import ConfigModel, Product
from contracts.vending_machine import SubsystemCapabilities
from services.dispenser_schema import (
    MECHANISM_FOR_KIND,
    SlotProfile,
    drive_channels,
    sense_channels,
    worst_case_seconds,
)
from services.paths import fsync_dir

_SLOT_PROFILE_ADAPTER = TypeAdapter(SlotProfile)

# tomllib already delivers `[slot.1]` as the string key "1"; this matches
# a bare, unsigned, non-zero-padded integer string exactly.
_SLOT_KEY_RE = re.compile(r"^(0|[1-9][0-9]*)$")

# tomllib's TOMLDecodeError message always ends in "(at line N, column M)".
_SYNTAX_LOCATION_RE = re.compile(r"at line (\d+), column (\d+)")

# Discriminator literal values used anywhere in the SlotProfile union tree.
# Pydantic error `loc` tuples are prefixed with these tags; `humanize`
# strips them so the reported path is just the dotted field path.
_DISCRIMINATOR_TAGS = frozenset(
    {
        "bagged_ice",
        "water_fill",
        "bag_full_sensor",
        "timed",
        "door_sensor",
        "flow_volume",
    }
)

# The two discriminator field names used anywhere in the SlotProfile tree.
_DISCRIMINATOR_FIELDS = ("mechanism", "proof")

_MECHANISM_LABELS = {
    "bagged_ice": "bagged ice",
    "water_fill": "water fill",
}


@dataclass(frozen=True)
class Finding:
    """One thing wrong (or worth a warning) with `dispensers.toml`.

    `slot` is `None` for a file-level finding (syntax, missing
    `schema_version`, no `[slot.N]` tables at all). `path` is a dotted
    field path within the slot's table (e.g. `"fill.max_run_seconds"`), or
    `""` for a finding about the slot/file as a whole.
    """

    slot: int | None
    path: str
    line: int | None
    severity: Literal["error", "warning"]
    message: str


@dataclass
class ValidationReport:
    """The outcome of one `validate_document` run."""

    findings: list[Finding] = field(default_factory=list)
    profiles: dict[int, SlotProfile] = field(default_factory=dict)
    file_error: bool = False

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def slots_examined(self) -> set[int]:
        examined = set(self.profiles)
        examined.update(f.slot for f in self.findings if f.slot is not None)
        return examined

    def for_slot(self, slot: int) -> list[Finding]:
        return [f for f in self.findings if f.slot == slot]

    def render_text(self) -> str:
        lines: list[str] = []
        for f in self.findings:
            prefix = "File" if f.slot is None else f"Slot {f.slot}"
            path_part = f" › {f.path}" if f.path else ""
            line_part = f" (line {f.line})" if f.line is not None else ""
            lines.append(f"{prefix}{path_part}{line_part}: {f.message}")

        for slot in sorted(self.slots_examined):
            slot_errors = self.for_slot(slot)
            slot_errors = [f for f in slot_errors if f.severity == "error"]
            profile = self.profiles.get(slot)
            if profile is not None:
                mechanism_label = _MECHANISM_LABELS.get(
                    profile.mechanism, profile.mechanism
                )
                label = f" ({profile.product_sku}, {mechanism_label})"
            else:
                label = ""
            if slot_errors:
                noun = "error" if len(slot_errors) == 1 else "errors"
                lines.append(f"Slot {slot}{label}: INVALID ({len(slot_errors)} {noun})")
            else:
                lines.append(f"Slot {slot}{label}: OK")

        if not self.findings:
            lines.append("OK")
        else:
            lines.append(
                f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)"
            )

        return "\n".join(lines)


def _file_finding(severity: Literal["error", "warning"], message: str) -> Finding:
    """Helper to construct a file-level `Finding` (no slot, path, or line)."""
    return Finding(slot=None, path="", line=None, severity=severity, message=message)


def _unwrap_annotated(tp: object) -> tuple[object, tuple]:
    """Strip one layer of `Annotated[...]`, returning (real_type, metadata)."""

    if hasattr(tp, "__metadata__"):
        args = typing.get_args(tp)
        return args[0], args[1:]
    return tp, ()


def _union_variants(tp: object) -> tuple | None:
    """Return a Union's member types, or `None` if `tp` is not a union."""

    origin = typing.get_origin(tp)
    if origin is types.UnionType or origin is typing.Union:
        return typing.get_args(tp)
    return None


def _discriminator_field(model_cls: type[BaseModel]):
    for name in _DISCRIMINATOR_FIELDS:
        if name in model_cls.model_fields:
            return model_cls.model_fields[name]
    return None


def _navigate(loc: Sequence) -> tuple[type[BaseModel] | None, object | None]:
    """Best-effort walk of the `SlotProfile` model tree following a
    Pydantic error `loc` tuple, discriminator tags included.

    Returns `(parent_model_cls, field_info)`: `parent_model_cls` is the
    last model class whose `model_fields` was consulted (used for
    "unknown field" suggestions); `field_info` is the resolved `FieldInfo`
    for the final token when it names a real field on that model, else
    `None`.
    """

    current, _ = _unwrap_annotated(SlotProfile)
    parent_model: type[BaseModel] | None = None
    field_info = None

    for token in loc:
        origin = typing.get_origin(current)
        if origin is dict:
            args = typing.get_args(current)
            if len(args) != 2:
                return parent_model, None
            # The loc token is the dict key itself (e.g. an accessory
            # name, as in `dict[str, Accessory]`), not a field name --
            # consume it and continue navigating into the value type.
            current, _ = _unwrap_annotated(args[1])
            if isinstance(current, type) and issubclass(current, BaseModel):
                parent_model = current
            field_info = None
            continue

        variants = _union_variants(current)
        if variants is not None:
            matched = None
            for variant in variants:
                variant_type, _ = _unwrap_annotated(variant)
                if not (
                    isinstance(variant_type, type)
                    and issubclass(variant_type, BaseModel)
                ):
                    continue
                disc = _discriminator_field(variant_type)
                if disc is None:
                    continue
                if token in typing.get_args(disc.annotation):
                    matched = variant_type
                    break
            if matched is None:
                return parent_model, None
            current = matched
            parent_model = matched
            field_info = None
            continue

        if isinstance(current, type) and issubclass(current, BaseModel):
            parent_model = current
            fields = current.model_fields
            if token not in fields:
                return parent_model, None
            field_info = fields[token]
            current, _ = _unwrap_annotated(field_info.annotation)
            continue

        return parent_model, None

    return parent_model, field_info


def _range_from_field_info(field_info) -> tuple[object | None, object | None, str]:
    """Read `ge`/`le` bounds and a unit off a `FieldInfo`, digging into a
    union's `Annotated` arm (the `"unmonitored"` fields) when the bounds
    are not on the field itself."""

    if field_info is None:
        return None, None, ""

    unit = ""
    if field_info.json_schema_extra:
        unit = field_info.json_schema_extra.get("unit", "")

    lo = hi = None

    def _scan(metadata) -> None:
        nonlocal lo, hi
        for m in metadata:
            if isinstance(m, annotated_types.Ge):
                lo = m.ge
            elif isinstance(m, annotated_types.Gt):
                lo = m.gt
            elif isinstance(m, annotated_types.Le):
                hi = m.le
            elif isinstance(m, annotated_types.Lt):
                hi = m.lt

    _scan(field_info.metadata)
    if lo is None and hi is None:
        for arg in typing.get_args(field_info.annotation):
            _, metadata = _unwrap_annotated(arg)
            _scan(metadata)

    return lo, hi, unit


def humanize(err: dict, slot: int) -> Finding:
    """Turn one Pydantic error dict (from
    `TypeAdapter(SlotProfile).validate_python`) into a `Finding`. `line`
    is always `None` here -- the caller locates it with `find_line` once
    it has the source text."""

    loc = tuple(err.get("loc", ()))
    # Strips by literal value, not position -- a table key that happens to
    # equal one of the tag strings (e.g. an accessory named "timed") would
    # be dropped from the reported path too. Deliberate per the plan.
    path = ".".join(str(part) for part in loc if part not in _DISCRIMINATOR_TAGS)
    err_type = err.get("type")
    message = err.get("msg", "")

    if err_type == "extra_forbidden":
        field_name = str(loc[-1]) if loc else ""
        parent_model, _ = _navigate(loc[:-1])
        suggestion = ""
        if parent_model is not None:
            matches = get_close_matches(
                field_name, list(parent_model.model_fields.keys())
            )
            if matches:
                suggestion = f" (did you mean {matches[0]}?)"
        message = f'unknown field "{field_name}"{suggestion}'

    elif err_type == "missing":
        field_name = str(loc[-1]) if loc else ""
        message = f'missing required field "{field_name}"'

    elif err_type in ("greater_than_equal", "less_than_equal"):
        _, field_info = _navigate(loc)
        lo, hi, unit = _range_from_field_info(field_info)
        if lo is not None and hi is not None:
            unit_part = f" {unit}" if unit else ""
            message = (
                f"must be between {lo} and {hi}{unit_part}, got {err.get('input')}"
            )
        # else: fall back to Pydantic's own message, already set above.

    elif err_type == "literal_error":
        expected = err.get("ctx", {}).get("expected", "")
        message = f'must be one of {expected}, got "{err.get("input")}"'

    elif err_type == "union_tag_invalid":
        ctx = err.get("ctx", {})
        discriminator = str(ctx.get("discriminator", "")).strip("'\"")
        expected_tags = ctx.get("expected_tags", "")
        message = (
            f'{discriminator} must be one of {expected_tags}, got "{ctx.get("tag")}"'
        )

    # else: every other error type keeps Pydantic's own `msg` verbatim.

    return Finding(slot=slot, path=path, line=None, severity="error", message=message)


def find_line(text: str, slot: int, path: str) -> int | None:
    """Best-effort source line for a `(slot, path)` finding: locate the
    `[slot.N...]` header for the deepest table named in `path`, then the
    first `key = ...` line after it. `None` when either can't be found."""

    if not path:
        return None

    parts = path.split(".")
    key = parts[-1]
    table_parts = parts[:-1]
    header = (
        f"[slot.{slot}" + ("." + ".".join(table_parts) if table_parts else "") + "]"
    )

    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.split("#", 1)[0].strip() == header:
            header_idx = i
            break
    if header_idx is None:
        return None

    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for j in range(header_idx + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("["):
            break
        if key_re.match(lines[j]):
            return j + 1

    return None


def _line_for_toml_error(exc: tomllib.TOMLDecodeError) -> int | None:
    match = _SYNTAX_LOCATION_RE.search(str(exc))
    return int(match.group(1)) if match else None


def validate_document(
    text: str,
    products: Sequence[Product],
    capabilities: SubsystemCapabilities | None = None,
    dispense_timeout_seconds: float = 120.0,
) -> ValidationReport:
    """Run the full §5.2 pipeline over `text` and return a report. Never
    raises on malformed input -- every failure becomes a `Finding`."""

    report = ValidationReport()

    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        report.findings.append(
            replace(_file_finding("error", str(exc)), line=_line_for_toml_error(exc))
        )
        report.file_error = True
        return report
    except (RecursionError, ValueError) as exc:
        # tomllib's parser recurses per nesting level; a pathologically
        # deep structure (thousands of `[[[...]]]`) can blow the
        # interpreter's recursion limit instead of raising
        # TOMLDecodeError. Never let that escape -- it's still just a bad
        # file, not a reason to crash the machine.
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        report.findings.append(
            _file_finding("error", f"dispensers.toml could not be parsed: {first_line}")
        )
        report.file_error = True
        return report

    schema_version = doc.get("schema_version")
    if schema_version != 1:
        report.findings.append(
            _file_finding("error", f"schema_version must be 1, got {schema_version!r}")
        )

    slot_table = doc.get("slot")
    if not isinstance(slot_table, dict):
        report.findings.append(_file_finding("error", "no [slot.N] tables found"))
        report.file_error = True
        return report

    slot_tables: dict[int, dict] = {}
    for key, table in slot_table.items():
        if not _SLOT_KEY_RE.match(key):
            report.findings.append(_file_finding("error", f'invalid slot key "{key}"'))
            continue
        slot_tables[int(key)] = table

    for slot_num, table in slot_tables.items():
        try:
            profile = _SLOT_PROFILE_ADAPTER.validate_python(table)
        except ValidationError as exc:
            for err in exc.errors():
                finding = humanize(err, slot_num)
                line = find_line(text, slot_num, finding.path)
                report.findings.append(replace(finding, line=line))
            continue
        report.profiles[slot_num] = profile

    _cross_check(report, products, capabilities, dispense_timeout_seconds, text)

    return report


def _cross_check(
    report: ValidationReport,
    products: Sequence[Product],
    capabilities: SubsystemCapabilities | None,
    dispense_timeout_seconds: float,
    text: str,
) -> None:
    """Pipeline step 4: cross-checks over the valid per-slot profiles and
    the product catalog. Operates on a snapshot of `report.profiles`
    (schema-valid slots only) so every sub-check sees the same candidates
    regardless of order; a slot that gains any cross-check error here is
    removed from `report.profiles` at the end."""

    candidates = dict(report.profiles)
    catalog_by_sku = {p.sku: p for p in products}
    error_slots: set[int] = set()

    def add_error(slot: int, path: str, message: str) -> None:
        report.findings.append(
            Finding(
                slot=slot,
                path=path,
                line=find_line(text, slot, path) if path else None,
                severity="error",
                message=message,
            )
        )
        error_slots.add(slot)

    # Every ice/water catalog product needs exactly one valid slot whose
    # product_sku matches, at that product's own slot number.
    for product in products:
        if product.kind not in ("ice", "water"):
            continue
        matched = any(p.product_sku == product.sku for p in candidates.values())
        if not matched:
            report.findings.append(
                Finding(
                    slot=product.slot,
                    path="",
                    line=None,
                    severity="error",
                    message=(
                        f'product "{product.sku}" (kind {product.kind}, '
                        f"slot {product.slot}) has no valid "
                        f"[slot.{product.slot}] table"
                    ),
                )
            )

    # Per-profile product checks: sku known, slot agrees, mechanism agrees.
    sku_slots: dict[str, list[int]] = {}
    for slot_num, profile in candidates.items():
        sku_slots.setdefault(profile.product_sku, []).append(slot_num)

        product = catalog_by_sku.get(profile.product_sku)
        if product is None:
            add_error(
                slot_num,
                "product_sku",
                f'product_sku "{profile.product_sku}" is not in the catalog',
            )
            continue

        if product.slot != slot_num:
            add_error(
                slot_num,
                "product_sku",
                f'product "{profile.product_sku}" is slot {product.slot} in the '
                f"catalog, not slot {slot_num}",
            )

        expected_mechanism = MECHANISM_FOR_KIND.get(product.kind)
        if profile.mechanism != expected_mechanism:
            expected_kind = next(
                (k for k, m in MECHANISM_FOR_KIND.items() if m == profile.mechanism),
                profile.mechanism,
            )
            add_error(
                slot_num,
                "mechanism",
                f'mechanism "{profile.mechanism}" needs a product of kind '
                f'"{expected_kind}", but "{profile.product_sku}" is kind '
                f'"{product.kind}"',
            )

    for sku, slots in sku_slots.items():
        if len(slots) > 1:
            for slot_num in slots:
                add_error(
                    slot_num,
                    "product_sku",
                    f'product_sku "{sku}" is used in slots {sorted(slots)}; a sku '
                    "may only appear in one slot",
                )

    # Channel roles: a channel driven by one slot can never be sensed by
    # any slot (sharing a drive channel between slots is fine).
    drive_users: dict[str, list[int]] = {}
    sense_users: dict[str, list[int]] = {}
    for slot_num, profile in candidates.items():
        for channel_id in drive_channels(profile):
            drive_users.setdefault(channel_id, []).append(slot_num)
        for channel_id in sense_channels(profile):
            sense_users.setdefault(channel_id, []).append(slot_num)

    for channel_id in set(drive_users) & set(sense_users):
        affected = set(drive_users[channel_id]) | set(sense_users[channel_id])
        for slot_num in affected:
            add_error(
                slot_num,
                "",
                f'channel "{channel_id}" is used as both a drive and a sensor',
            )

    # Time budget: worst case must fit the configured timeout minus margin.
    margin = dispense_timeout_seconds - 5
    for slot_num, profile in candidates.items():
        worst = worst_case_seconds(profile)
        if worst > margin:
            add_error(
                slot_num,
                "",
                f"worst-case dispense of {worst} s exceeds dispense_timeout_seconds "
                f"{dispense_timeout_seconds} s minus the 5 s margin",
            )

    # Capabilities: when the vending board is known, every drive channel
    # must be an output and every sense channel an input. Unknown board ->
    # one warning per valid slot, not an error.
    if capabilities is None:
        for slot_num in candidates:
            if slot_num in error_slots:
                continue
            report.findings.append(
                Finding(
                    slot=slot_num,
                    path="",
                    line=None,
                    severity="warning",
                    message="board capabilities unknown; channel names not verified",
                )
            )
    else:
        direction_by_id = {c.channel_id: c.direction for c in capabilities.channels}
        for slot_num, profile in candidates.items():
            for channel_id in drive_channels(profile):
                _check_declared_direction(
                    add_error, slot_num, channel_id, direction_by_id, "output", "drive"
                )
            for channel_id in sense_channels(profile):
                _check_declared_direction(
                    add_error, slot_num, channel_id, direction_by_id, "input", "sensor"
                )

    for slot_num in error_slots:
        report.profiles.pop(slot_num, None)


def _check_declared_direction(
    add_error,
    slot_num: int,
    channel_id: str,
    direction_by_id: dict[str, str],
    expected_direction: str,
    role: str,
) -> None:
    direction = direction_by_id.get(channel_id)
    if direction is None:
        add_error(
            slot_num,
            "",
            f'channel "{channel_id}" is not declared by the vending board',
        )
    elif direction != expected_direction:
        add_error(
            slot_num,
            "",
            f'channel "{channel_id}" is declared as {direction}, but this profile '
            f"uses it as {role}",
        )


def dispensers_path() -> Path:
    """Resolve the active `dispensers.toml` path from
    `ICE_COLDER_DISPENSERS` (read at call time, mirroring
    `main._config_path`), defaulting to `dispensers.toml` in the current
    working directory."""

    return Path(os.environ.get("ICE_COLDER_DISPENSERS", "dispensers.toml"))


class DispenserProfiles:
    """Load/validate/save service for the hand-edited `dispensers.toml`.

    Wraps `validate_document` with the live product catalog and dispense
    timeout read off `config` on every validation (so a catalog edit is
    picked up on the next `load()`), plus an atomic, digest-guarded save.
    This class does no TOML/schema validation of its own -- that is
    `validate_document`'s job; this is file I/O and state.
    """

    def __init__(self, config: ConfigModel, path: Path | None = None) -> None:
        self.config = config
        self.path = path or dispensers_path()
        self.report: ValidationReport = ValidationReport()
        self.capabilities: SubsystemCapabilities | None = None
        self.digest: str | None = None
        self._text: str | None = None
        self._loaded: bool = False

    def load(self) -> ValidationReport:
        """Read `self.path` and validate it, storing `report` and
        `digest`. A missing file is a single file-level warning (not an
        exception); a directory at that path raises `IsADirectoryError`
        for the caller (`main.py`) to turn into a clean exit."""

        if self.path.is_dir():
            raise IsADirectoryError(f"{self.path} is a directory, not a file")

        if not self.path.exists():
            self._text = None
            self.digest = None
            self._loaded = True
            self.report = ValidationReport(
                findings=[
                    _file_finding(
                        "warning",
                        (
                            f"dispensers.toml not found at {self.path}; no "
                            "products have a dispenser profile"
                        ),
                    )
                ],
                profiles={},
                file_error=True,
            )
            return self.report

        try:
            raw = self.path.read_bytes()
        except IsADirectoryError:
            raise
        except OSError as exc:
            # A permission error or other OS-level read failure must
            # never crash startup -- fold it into a file-level finding
            # like any other bad dispensers.toml. Deliberately leaves
            # `_loaded` False: we never actually saw the file's bytes,
            # so there is nothing to compute a digest from, and a later
            # `save_text` must refuse rather than treat this as "loaded
            # with no changes since".
            first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            self._text = None
            self.digest = None
            self.report = ValidationReport(
                findings=[
                    _file_finding(
                        "error", f"dispensers.toml could not be read: {first_line}"
                    )
                ],
                profiles={},
                file_error=True,
            )
            return self.report

        self.digest = hashlib.sha256(raw).hexdigest()
        self._loaded = True

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self._text = None
            self.report = ValidationReport(
                findings=[
                    _file_finding(
                        "error",
                        f"dispensers.toml is not valid UTF-8 text (byte {exc.start}): {exc.reason}",
                    )
                ],
                profiles={},
                file_error=True,
            )
            return self.report

        self._text = text
        self.report = self.validate_text(text)
        return self.report

    def validate_text(self, text: str) -> ValidationReport:
        """Pure validation of `text` against the live catalog/timeout/
        capabilities -- writes nothing, and does not touch `self.report`."""

        return validate_document(
            text,
            self.config.products,
            capabilities=self.capabilities,
            dispense_timeout_seconds=self.config.physical.dispense_timeout_seconds,
        )

    def save_text(self, text: str, expected_digest: str | None) -> ValidationReport:
        """Validate and atomically persist `text`, refusing (file
        untouched) if the profiles were never loaded, `expected_digest` is
        stale, or the text has errors."""

        if not self._loaded:
            return ValidationReport(
                findings=[
                    _file_finding(
                        "error",
                        "profiles were never loaded; call load() before saving",
                    )
                ]
            )

        if expected_digest != self.digest:
            return ValidationReport(
                findings=[
                    _file_finding(
                        "error",
                        "file changed on disk since you opened it; reload the page",
                    )
                ]
            )

        report = self.validate_text(text)
        if not report.ok:
            return report

        tmp = self.path.with_suffix(".toml.tmp")
        bak = self.path.with_suffix(".toml.bak")
        try:
            # Binary mode: a text-mode write translates "\n" to the
            # platform line ending (CRLF on Windows), which would make the
            # on-disk digest depend on the OS the save ran on. Writing the
            # exact UTF-8 bytes keeps `digest` predictable across
            # platforms.
            with open(tmp, "wb") as f:
                f.write(text.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            # Copy (not rename) the live file to .bak *before* the single
            # replace below, so the live path is never briefly absent --
            # matching services/config_store.py's save_config.
            if self.path.exists():
                shutil.copy2(self.path, bak)
            os.replace(tmp, self.path)
            fsync_dir(self.path.parent)
        finally:
            if tmp.exists():
                tmp.unlink()

        return self.load()

    def profile_for_slot(self, slot: int) -> SlotProfile | None:
        return self.report.profiles.get(slot)

    def set_capabilities(self, doc: SubsystemCapabilities | None) -> ValidationReport:
        """Store `doc` and re-run validation on the last-loaded text (not
        the file on disk) so capability warnings resolve without a save."""

        self.capabilities = doc
        if self._text is None:
            return self.report
        self.report = self.validate_text(self._text)
        return self.report


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for dispensers validation.

    Returns:
        0: validation ok (or --example)
        1: validation has errors
        2: directory path or unreadable config
    """
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m services.dispensers",
        description="Validate dispensers.toml against the product catalog",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="Path to dispensers.toml (default: $ICE_COLDER_DISPENSERS or dispensers.toml)",
    )
    parser.add_argument(
        "--config",
        help="Path to config.json (default: $ICE_COLDER_CONFIG or config.json)",
    )
    parser.add_argument(
        "--capabilities",
        metavar="FILE",
        help="Path to JSON file with SubsystemCapabilities",
    )

    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--example",
        action="store_true",
        help="Print example dispensers.toml and exit",
    )
    action_group.add_argument(
        "--check",
        action="store_true",
        help="Check dispensers.toml (default action)",
    )

    args = parser.parse_args(argv)

    # Handle --example first (no file I/O needed)
    if args.example:
        from services.dispensers_doc import render_example

        example_text = render_example()
        sys.stdout.write(example_text)
        return 0

    # Resolve paths
    dispensers_file_path = dispensers_path() if args.path is None else Path(args.path)
    config_path_str = args.config or os.environ.get("ICE_COLDER_CONFIG", "config.json")

    # Load config (try block for config errors)
    try:
        with open(config_path_str, "r", encoding="utf-8") as f:
            config_data = json.load(f)
        config = ConfigModel.model_validate(config_data)
    except ValidationError as exc:
        error_msg = f"{exc.error_count()} validation error(s) in {config_path_str}"
        print(f"Error reading config: {error_msg}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        error_msg = str(exc).splitlines()[0]
        print(f"Error reading config: {error_msg}", file=sys.stderr)
        return 2

    # Load dispensers file and check for directory
    profiles = DispenserProfiles(config, dispensers_file_path)
    try:
        report = profiles.load()
    except OSError as exc:
        error_msg = str(exc).splitlines()[0]
        print(f"Error reading dispensers file: {error_msg}", file=sys.stderr)
        return 2

    # Load capabilities if provided
    if args.capabilities:
        try:
            with open(args.capabilities, "r", encoding="utf-8") as f:
                caps_data = json.load(f)
            caps = SubsystemCapabilities.model_validate(caps_data)
            report = profiles.set_capabilities(caps)
        except ValidationError as exc:
            error_msg = (
                f"{exc.error_count()} validation error(s) in {args.capabilities}"
            )
            print(f"Error reading capabilities: {error_msg}", file=sys.stderr)
            return 2
        except (OSError, ValueError) as exc:
            error_msg = str(exc).splitlines()[0]
            print(f"Error reading capabilities: {error_msg}", file=sys.stderr)
            return 2

    # Print report and return status
    output = report.render_text()
    print(output)

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
