"""Export and restore a bank's parser configuration as a portable manifest.

A manifest records everything that can change parsed output: parser options,
extra transaction boundaries, tag overrides, customised processor slots and the
built-in environment those overrides extend. Built-in tags and processors are
identified by digests, not copied as live objects. A SHA-256 content version
covers every recorded field, so an edited manifest is rejected before it is
used to parse a statement.

Rules are namespaced by bank. Two banks may override the same tag or register
a processor with the same function name without sharing storage or resolution
state. Serialized rules carry identifiers such as ``acme-bank:tag:61`` and
``acme-bank:processor:post_statement:2``.

Restoring is strict. A tag or processor that cannot be imported or explicitly
bound raises :class:`ManifestError` listing every unavailable rule, its reason
and the output fields it would have supplied. No missing rule is replaced by a
built-in default.

Example:
    >>> import mt940
    >>> manifest = mt940.manifest.export_manifest('acme')
    >>> manifest['bank'], manifest['schema']
    ('acme', 'mt940.bank-manifest/1')
    >>> manifest['content_version'].startswith('sha256:')
    True
    >>> mt940.manifest.restore_manifest(manifest).bank
    'acme'
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import importlib
import inspect
import json
import pathlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from . import models, processors
from . import tags as tags_module
from .options import Options

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from ._types import Processor, Processors, Source

#: Manifest schema identifier. Readers reject any other value.
SCHEMA = "mt940.bank-manifest/1"

#: Output fields produced by each built-in tag slug.
_TAG_FIELDS: dict[str, tuple[str, ...]] = {
    "date_time_indication": ("date",),
    "transaction_reference_number": ("transaction_reference",),
    "related_reference": ("related_reference",),
    "account_identification": ("account_identification",),
    "statement_number": ("statement_number", "sequence_number"),
    "floor_limit_indicator": ("d_floor_limit", "c_floor_limit"),
    "opening_balance": ("opening_balance",),
    "intermediate_opening_balance": ("intermediate_opening_balance",),
    "final_opening_balance": ("final_opening_balance",),
    "statement": (
        "date",
        "entry_date",
        "guessed_entry_date",
        "amount",
        "id",
        "customer_reference",
        "bank_reference",
        "extra_details",
        "funds_code",
        "status",
        "currency",
    ),
    "closing_balance": ("closing_balance",),
    "intermediate_closing_balance": ("intermediate_closing_balance",),
    "final_closing_balance": ("final_closing_balance",),
    "available_balance": ("available_balance",),
    "forward_available_balance": ("forward_available_balance",),
    "transaction_details": tuple(
        dict.fromkeys(
            (
                *processors.DETAIL_KEYS.values(),
                *processors.DETAIL_KEYS_APPLICANT_IBAN.values(),
                *processors.GVC_KEYS.values(),
            )
        )
    ),
    "non_swift": ("non_swift", "non_swift_text", "non_swift_<id>"),
    "sum_entries": ("sum_entries",),
    "sum_debit_entries": ("sum_debit_entries",),
    "sum_credit_entries": ("sum_credit_entries",),
}

#: Static field impact for package factories and processors.
_PROCESSOR_FIELDS: dict[str, tuple[str, ...]] = {
    "mt940.processors.add_currency_pre_processor": ("currency",),
    "mt940.processors.date_cleanup_post_processor": (
        "day",
        "month",
        "year",
        "entry_day",
        "entry_month",
    ),
    "mt940.processors.date_fixup_pre_processor": ("day",),
    "mt940.processors.mBank_set_iph_id": ("iph_id",),
    "mt940.processors.mBank_set_tnr": ("tnr",),
    "mt940.processors.mBank_set_transaction_code": ("transaction_code",),
    "mt940.processors.transaction_details_post_processor": _TAG_FIELDS[
        "transaction_details"
    ],
}

#: Stable scope markers, referenced by name.
_SCOPES: dict[str, type[models.Transactions | models.Transaction]] = {
    "both": models.TransactionsAndTransaction,
    "statement": models.Transactions,
    "transaction": models.Transaction,
}


@dataclasses.dataclass(frozen=True)
class ManifestIssue:
    """One reason a manifest cannot reproduce its recorded output.

    Attributes:
        bank: Bank namespace whose rule failed.
        kind: ``content_version``, ``environment``, ``tag``, ``processor``,
            ``options`` or ``bank``.
        rule: Bank-qualified rule identifier, or the bank name for a conflict.
        reason: Human-readable explanation.
        affected_fields: Output fields the missing rule would have supplied.
            ``('*',)`` means the impact could not be narrowed down.
    """

    bank: str
    kind: str
    rule: str
    reason: str
    affected_fields: tuple[str, ...]

    def __str__(self) -> str:
        """Format the issue with its bank, rule and affected fields."""
        fields = ", ".join(self.affected_fields) or "unknown"
        return (
            f"{self.bank}: {self.kind} {self.rule!r} ({self.reason}); "
            f"affected fields: {fields}"
        )


class ManifestError(RuntimeError):
    """Raised when a manifest cannot be reproduced without changing output.

    Attributes:
        bank: Bank namespace being restored.
        issues: Every problem found, not only the first one.
    """

    def __init__(self, bank: str, issues: Sequence[ManifestIssue]) -> None:
        """Build an error listing every issue and its affected fields.

        Args:
            bank: Bank namespace being restored.
            issues: Problems collected while resolving the manifest.
        """
        self.bank: str = bank
        self.issues: tuple[ManifestIssue, ...] = tuple(issues)
        lines = [f"cannot reproduce manifest for bank {bank!r}:"]
        lines.extend(f"  - {issue}" for issue in self.issues)
        super().__init__("\n".join(lines))


@dataclasses.dataclass(frozen=True)
class RestoredConfig:
    """Verified parser configuration reconstructed from a manifest.

    Attributes:
        bank: Bank namespace that owned the manifest.
        content_version: Digest recorded by the manifest.
        tags: Complete tag-ID mapping, including built-in entries.
        processors: Complete processor slot mapping.
        options: Parser switches recorded by the manifest.
        transaction_boundary: Extra transaction-start tag slugs.
    """

    bank: str
    content_version: str
    tags: dict[int | str, tags_module.Tag]
    processors: Processors
    options: Options
    transaction_boundary: frozenset[str]

    def parse(
        self, src: Source, encoding: str | None = None
    ) -> models.Transactions:
        """Parse one source with exactly this configuration.

        Args:
            src: Source accepted by :func:`mt940.parse`.
            encoding: Preferred byte decoding.

        Returns:
            The populated collection.
        """
        from .parser import parse

        return parse(
            src,
            encoding,
            dict(self.processors),
            dict(self.tags),
            self.transaction_boundary,
            options=self.options,
        )

    def parse_statements(
        self, src: Source, encoding: str | None = None
    ) -> list[models.Transactions]:
        """Parse each ``:20:`` block with exactly this configuration.

        Args:
            src: Source accepted by :func:`mt940.parse_statements`.
            encoding: Preferred byte decoding.

        Returns:
            One collection per statement, in source order.
        """
        from .parser import parse_statements

        return parse_statements(
            src,
            encoding,
            dict(self.processors),
            dict(self.tags),
            self.transaction_boundary,
            options=self.options,
        )


class ManifestRegistry:
    """Keep bank manifests separate, including identically named rules.

    Each bank has its own namespace. Adding another manifest for a bank with a
    different content version raises :class:`ManifestError` unless replacement
    is explicit.

    Example:
        >>> registry = ManifestRegistry()
        >>> registry.add(export_manifest('bank-a'))
        >>> registry.add(export_manifest('bank-b'))
        >>> registry.banks
        ('bank-a', 'bank-b')
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        #: Bank namespace to stored manifest mapping.
        self._manifests: dict[str, dict[str, Any]] = {}

    @property
    def banks(self) -> tuple[str, ...]:
        """Return stored bank names in sorted order."""
        return tuple(sorted(self._manifests))

    def add(
        self, manifest: Mapping[str, Any], *, replace: bool = False
    ) -> None:
        """Store one bank-namespaced manifest.

        Args:
            manifest: Manifest produced by :func:`export_manifest`.
            replace: Replace an existing manifest for the same bank instead of
                rejecting a content-version conflict.

        Raises:
            ManifestError: The bank already has a different content version
                and ``replace`` is false.
            TypeError: The manifest or one of its fields has a wrong type.
            ValueError: The schema or an option name is invalid.
        """
        _validate_manifest(manifest)
        bank = _bank(manifest)
        existing = self._manifests.get(bank)
        if (
            existing is not None
            and not replace
            and existing["content_version"] != manifest["content_version"]
        ):
            raise ManifestError(
                bank,
                [
                    ManifestIssue(
                        bank,
                        "bank",
                        bank,
                        "conflicting content version for the same bank",
                        _all_fields(manifest),
                    )
                ],
            )
        self._manifests[bank] = dict(manifest)

    def get(self, bank: str) -> dict[str, Any]:
        """Return a shallow copy of one bank's manifest.

        Args:
            bank: Exact bank namespace to retrieve.

        Returns:
            The stored manifest mapping.

        Raises:
            KeyError: No manifest is stored for the bank.
        """
        return dict(self._manifests[bank])

    def verify(self, bank: str) -> list[ManifestIssue]:
        """Check one bank's manifest without restoring it.

        Args:
            bank: Exact bank namespace to inspect.

        Returns:
            Every resolution issue.

        Raises:
            KeyError: No manifest is stored for the bank.
        """
        return verify_manifest(self._manifests[bank])

    def restore(self, bank: str) -> RestoredConfig:
        """Restore one bank's configuration without consulting other banks.

        Args:
            bank: Exact bank namespace to restore.

        Returns:
            The verified configuration.

        Raises:
            KeyError: No manifest is stored for the bank.
            ManifestError: Any rule is missing, changed or tampered with.
        """
        return restore_manifest(self._manifests[bank])


def export_manifest(
    bank: str,
    *,
    tags: Mapping[int | str, tags_module.Tag] | None = None,
    processors: Processors | None = None,
    options: Options | None = None,
    transaction_boundary: Iterable[str] | None = None,
    removed_tags: Iterable[int | str] = (),
    removed_processor_slots: Iterable[str] = (),
) -> dict[str, Any]:
    """Export a bank's parser configuration as a versioned manifest.

    Args:
        bank: Namespace for every rule. Colons, slashes, whitespace and an
            empty name are rejected.
        tags: Tag overrides keyed by ID. Built-in instances are omitted
            because they are bound through ``environment``.
        processors: Processor slots to record. A slot whose list is identical
            to :attr:`mt940.models.Transactions.DEFAULT_PROCESSORS` is omitted.
        options: Parser switches. Omitted disables all ten switches.
        transaction_boundary: Extra transaction-start slugs. Omitted is empty.
        removed_tags: Built-in tag IDs to remove on restore.
        removed_processor_slots: Built-in processor slots to empty on restore.

    Returns:
        A JSON-compatible manifest with schema, bank, content version,
        environment digests, options, tag overrides, processor dependencies
        and transaction boundaries.

    Raises:
        ValueError: The bank name is invalid, or a removal names something
            that is not a built-in.
        TypeError: A tag, option or removal value has an unsupported type.
    """
    _validate_bank(bank)
    payload: dict[str, Any] = {
        "bank": bank,
        "environment": _environment(),
        "mt940_version": _version(),
        "options": _record_options(Options() if options is None else options),
        "processor_slots": _slot_overrides(
            processors
            if processors is not None
            else models.Transactions.DEFAULT_PROCESSORS
        ),
        "removed_processor_slots": _removed_slots(removed_processor_slots),
        "removed_tag_ids": _removed_tags(removed_tags),
        "schema": SCHEMA,
        "tag_overrides": _tag_overrides(tags or {}),
        "transaction_boundary": sorted(frozenset(transaction_boundary or ())),
    }
    payload["content_version"] = _digest(payload)
    return payload


def capture_manifest(
    statement: models.Transactions, bank: str
) -> dict[str, Any]:
    """Export the exact configuration held by a parsed collection.

    Unlike :func:`export_manifest`, this also records tag IDs and processor
    slots that were removed from the collection.

    Args:
        statement: Collection whose configuration should be recorded.
        bank: Namespace for every rule.

    Returns:
        The versioned manifest mapping.

    Raises:
        ValueError: The bank name is invalid.
        TypeError: A stored tag or option value has an unsupported type.
    """
    _validate_bank(bank)
    defaults = models.Transactions.default_tags()
    payload: dict[str, Any] = {
        "bank": bank,
        "environment": _environment(),
        "mt940_version": _version(),
        "options": _record_options(statement.options),
        "processor_slots": _slot_overrides(statement.processors),
        "removed_processor_slots": sorted(
            set(models.Transactions.DEFAULT_PROCESSORS)
            - set(statement.processors)
        ),
        "removed_tag_ids": sorted(
            (tag_id for tag_id in defaults if tag_id not in statement.tags),
            key=str,
        ),
        "schema": SCHEMA,
        "tag_overrides": _tag_overrides(statement.tags),
        "transaction_boundary": sorted(statement.transaction_boundary),
    }
    payload["content_version"] = _digest(payload)
    return payload


def content_version(manifest: Mapping[str, Any]) -> str:
    """Recompute the digest covered by a manifest.

    Args:
        manifest: Manifest whose ``content_version`` field is ignored while
            hashing every other field.

    Returns:
        ``sha256:`` followed by the lowercase hexadecimal digest.
    """
    return _digest(
        {
            key: value
            for key, value in manifest.items()
            if key != "content_version"
        }
    )


def manifest_dumps(
    manifest: Mapping[str, Any], *, indent: int | None = 2
) -> str:
    """Serialize a manifest deterministically.

    Args:
        manifest: Manifest to serialize.
        indent: JSON indentation passed to :func:`json.dumps`.

    Returns:
        JSON text with sorted keys and non-ASCII characters preserved.
    """
    return json.dumps(
        manifest, indent=indent, sort_keys=True, ensure_ascii=False
    )


def manifest_loads(text: str) -> dict[str, Any]:
    """Parse manifest JSON and validate its structure.

    Args:
        text: JSON produced by :func:`manifest_dumps` or compatible tooling.

    Returns:
        The validated manifest.

    Raises:
        json.JSONDecodeError: The text is not valid JSON.
        TypeError: A manifest field has the wrong type.
        ValueError: The schema or an option name is invalid.
    """
    value: object = json.loads(text)
    if not isinstance(value, dict):
        message = "manifest JSON must be an object"
        raise TypeError(message)
    manifest: dict[str, Any] = value
    _validate_manifest(manifest)
    return manifest


def write_manifest(
    manifest: Mapping[str, Any], path: str | pathlib.Path
) -> None:
    """Write a manifest as UTF-8 JSON with a trailing newline.

    Args:
        manifest: Manifest to write.
        path: Destination file path.
    """
    _ = pathlib.Path(path).write_text(
        f"{manifest_dumps(manifest)}\n", encoding="utf-8"
    )


def read_manifest(path: str | pathlib.Path) -> dict[str, Any]:
    """Read and validate a manifest written by :func:`write_manifest`.

    Args:
        path: Manifest file path.

    Returns:
        The validated manifest.
    """
    return manifest_loads(pathlib.Path(path).read_text(encoding="utf-8"))


def verify_manifest(
    manifest: Mapping[str, Any],
    *,
    available_tags: Mapping[str, object] | None = None,
    available_processors: Mapping[str, object] | None = None,
) -> list[ManifestIssue]:
    """Report every rule that cannot be reproduced.

    The content version and environment are checked first, then every tag and
    processor. Resolution does not mutate the manifest and installs no partial
    defaults.

    Args:
        manifest: Manifest to check.
        available_tags: Explicit tag classes or instances keyed by
            bank-qualified rule, ``module.qualname`` or tag ID.
        available_processors: Explicit callables keyed by bank-qualified rule,
            ``module.qualname``, slot name or ``slot:index``.

    Returns:
        Issues in manifest order, with integrity issues first.

    Raises:
        TypeError: The manifest or one of its fields has a wrong type.
        ValueError: The schema or an option name is invalid.
    """
    _validate_manifest(manifest)
    issues: list[ManifestIssue] = []
    context = _Context(manifest, available_tags, available_processors, issues)
    _ = context.resolve()
    return issues


def restore_manifest(
    manifest: Mapping[str, Any],
    *,
    available_tags: Mapping[str, object] | None = None,
    available_processors: Mapping[str, object] | None = None,
) -> RestoredConfig:
    """Reconstruct a parser configuration, refusing partial fallbacks.

    Args:
        manifest: Manifest produced by :func:`export_manifest` or
            :func:`capture_manifest`.
        available_tags: Explicit tag classes or instances keyed by
            bank-qualified rule, ``module.qualname`` or tag ID.
        available_processors: Explicit callables keyed by bank-qualified rule,
            ``module.qualname``, slot name or ``slot:index``.

    Returns:
        The complete configuration.

    Raises:
        ManifestError: Any rule is missing, changed or tampered with. The
            error lists every issue and its affected fields.
        TypeError: The manifest or one of its fields has a wrong type.
        ValueError: The schema or an option name is invalid.
    """
    _validate_manifest(manifest)
    issues: list[ManifestIssue] = []
    context = _Context(manifest, available_tags, available_processors, issues)
    config = context.resolve()
    if config is None:
        raise ManifestError(_bank(manifest), issues)
    return config


class _Context:
    """Mutable resolution state for one manifest.

    Attributes:
        manifest: Manifest under resolution.
        bank: Validated bank namespace.
        available_tags: Explicit tag bindings supplied by the caller.
        available_processors: Explicit processor bindings.
        issues: Shared list receiving every resolution problem.
    """

    def __init__(
        self,
        manifest: Mapping[str, Any],
        available_tags: Mapping[str, object] | None,
        available_processors: Mapping[str, object] | None,
        issues: list[ManifestIssue],
    ) -> None:
        """Capture the manifest, explicit bindings and issue list."""
        self.manifest = manifest
        self.bank = _bank(manifest)
        self.available_tags = available_tags
        self.available_processors = available_processors
        self.issues = issues

    def resolve(self) -> RestoredConfig | None:
        """Resolve every rule, returning ``None`` when any issue was found."""
        self.check_integrity()
        options = self._options()
        tags = self._tags()
        processors_ = self._processors()
        if self.issues or tags is None or processors_ is None:
            return None
        return RestoredConfig(
            bank=self.bank,
            content_version=str(self.manifest["content_version"]),
            tags=tags,
            processors=processors_,
            options=options,
            transaction_boundary=frozenset(
                self.manifest["transaction_boundary"]
            ),
        )

    def check_integrity(self) -> None:
        """Check content version, environment and options."""
        recorded = str(self.manifest["content_version"])
        actual = content_version(self.manifest)
        if recorded != actual:
            self.issues.append(
                ManifestIssue(
                    self.bank,
                    "content_version",
                    f"{self.bank}:content",
                    f"digest {recorded!r} does not match {actual!r}",
                    _all_fields(self.manifest),
                )
            )
        for area, current in (
            ("tag", _tag_environment()),
            ("processor", _processor_environment()),
        ):
            for key, fingerprint in self.manifest["environment"][area].items():
                if current.get(key) == fingerprint:
                    continue
                if area == "tag":
                    fields = tuple(fingerprint.get("affected_fields", ("*",)))
                else:
                    fields = tuple(
                        field
                        for item in fingerprint
                        for field in item["affected_fields"]
                    )
                self.issues.append(
                    ManifestIssue(
                        self.bank,
                        "environment",
                        f"{self.bank}:{area}:{key}",
                        f"built-in {area} differs from the manifest",
                        fields or ("*",),
                    )
                )
        try:
            _ = _resolve_options(self.manifest["options"])
        except (TypeError, ValueError) as error:
            self.issues.append(
                ManifestIssue(
                    self.bank,
                    "options",
                    f"{self.bank}:options",
                    str(error),
                    ("*",),
                )
            )

    def _options(self) -> Options:
        """Return the recorded options, or defaults when they are invalid."""
        try:
            return _resolve_options(self.manifest["options"])
        except (TypeError, ValueError):
            return Options()

    def _tags(self) -> dict[int | str, tags_module.Tag] | None:
        """Build the complete tag mapping, or ``None`` after an issue."""
        mapping = dict(models.Transactions.default_tags())
        for tag_id in self.manifest["removed_tag_ids"]:
            _ = mapping.pop(tag_id, None)
        resolved = True
        for entry in self.manifest["tag_overrides"]:
            tag = self._resolve_tag(entry)
            if tag is None:
                resolved = False
                continue
            mapping[entry["id"]] = tag
        return mapping if resolved else None

    def _processors(self) -> Processors | None:
        """Build the complete processor mapping, or ``None`` after an issue."""
        mapping = {
            slot: list(callbacks)
            for slot, callbacks in (
                models.Transactions.DEFAULT_PROCESSORS.items()
            )
        }
        for slot in self.manifest["removed_processor_slots"]:
            mapping[slot] = []
        resolved = True
        for slot, entries in self.manifest["processor_slots"].items():
            callbacks: list[Processor] = []
            for index, entry in enumerate(entries):
                processor = self._resolve_processor(entry, slot, index)
                if processor is None:
                    resolved = False
                    continue
                callbacks.append(processor)
            mapping[slot] = callbacks
        return mapping if resolved else None

    def _resolve_tag(self, entry: Mapping[str, Any]) -> tags_module.Tag | None:
        """Resolve one tag override through binding or import."""
        rule = f"{self.bank}:tag:{entry['id']}"
        fields = tuple(entry["affected_fields"])
        candidate = _lookup(
            self.available_tags,
            (
                rule,
                f"{entry['class']['module']}.{entry['class']['qualname']}",
            ),
        )
        if candidate is None:
            try:
                candidate = _import_object(
                    entry["class"]["module"], entry["class"]["qualname"]
                )
            except (AttributeError, ImportError, ModuleNotFoundError):
                self.issues.append(
                    ManifestIssue(
                        self.bank,
                        "tag",
                        rule,
                        "tag class is not importable and was not bound",
                        fields,
                    )
                )
                return None
        instance = candidate() if isinstance(candidate, type) else candidate
        if not isinstance(instance, tags_module.Tag):
            self.issues.append(
                ManifestIssue(
                    self.bank,
                    "tag",
                    rule,
                    "binding is not a tag instance or class",
                    fields,
                )
            )
            return None
        mismatch = _tag_mismatch(instance, entry)
        if mismatch is not None:
            self.issues.append(
                ManifestIssue(self.bank, "tag", rule, mismatch, fields)
            )
            return None
        return instance

    def _resolve_processor(
        self, entry: Mapping[str, Any], slot: str, index: int
    ) -> Processor | None:
        """Resolve one processor through binding, built-in or import."""
        rule = f"{self.bank}:processor:{slot}:{index}"
        fields = tuple(entry["affected_fields"])
        candidate = _lookup(
            self.available_processors,
            (rule, entry["reference"]),
        )
        if candidate is None:
            candidate = _find_default_processor(entry)
        if candidate is None:
            try:
                candidate = _import_object(
                    entry["class"]["module"], entry["class"]["qualname"]
                )
            except (AttributeError, ImportError, ModuleNotFoundError):
                reason = "callable is not importable and was not bound"
                self.issues.append(
                    ManifestIssue(self.bank, "processor", rule, reason, fields)
                )
                return None
        if not callable(candidate):
            self.issues.append(
                ManifestIssue(
                    self.bank,
                    "processor",
                    rule,
                    "binding is not callable",
                    fields,
                )
            )
            return None
        processor = _apply_partial(candidate, entry["partial"])
        if processor is None:
            self.issues.append(
                ManifestIssue(
                    self.bank,
                    "processor",
                    rule,
                    "partial arguments are not serializable",
                    fields,
                )
            )
            return None
        if _callable_digest(processor) != entry["digest"]:
            self.issues.append(
                ManifestIssue(
                    self.bank,
                    "processor",
                    rule,
                    "callable digest differs from the manifest",
                    fields,
                )
            )
            return None
        return processor


def _environment() -> dict[str, Any]:
    """Fingerprint the built-in tags and processors being extended."""
    return {
        "processor": _processor_environment(),
        "tag": _tag_environment(),
    }


def _tag_environment() -> dict[str, dict[str, Any]]:
    """Fingerprint every built-in tag in the running package."""
    result: dict[str, dict[str, Any]] = {}
    for tag_id, tag in models.Transactions.default_tags().items():
        fields, known = _tag_fields(tag)
        result[str(tag_id)] = {
            "affected_fields": list(fields),
            "digest": _tag_digest(tag),
            "fields_known": known,
            "slug": tag.slug,
        }
    return result


def _processor_environment() -> dict[str, list[dict[str, Any]]]:
    """Fingerprint every built-in processor slot in the running package."""
    return {
        slot: [_processor_fingerprint(processor) for processor in callbacks]
        for slot, callbacks in (models.Transactions.DEFAULT_PROCESSORS.items())
    }


def _tag_digest(tag: tags_module.Tag) -> str:
    """Hash a built-in tag's declared shape and conversion source."""
    cls = type(tag)
    return _digest(
        {
            "call": _callable_digest(cls.__call__),
            "flags": int(tag.RE_FLAGS),
            "id": tag.id,
            "parse": _callable_digest(cls.parse),
            "pattern": tag.pattern,
            "scope": _scope_record(tag.scope),
            "slug": tag.slug,
        }
    )


def _processor_fingerprint(processor: Processor) -> dict[str, Any]:
    """Describe a processor by reference, digest and field impact."""
    module, qualname, reference = _callable_identity(processor)
    fields, known = _processor_fields(processor)
    return {
        "affected_fields": list(fields),
        "digest": _callable_digest(processor),
        "fields_known": known,
        "module": module,
        "qualname": qualname,
        "reference": reference,
    }


def _tag_overrides(
    mapping: Mapping[int | str, tags_module.Tag],
) -> list[dict[str, Any]]:
    """Serialize only tags that are not the shared built-in instances."""
    entries: list[dict[str, Any]] = []
    for tag_id in _sorted_ids(mapping):
        tag = mapping[tag_id]
        if tag is tags_module.TAG_BY_ID.get(tag_id):
            continue
        fields, known = _tag_fields(tag)
        entries.append(
            {
                "affected_fields": list(fields),
                "class": _class_reference(type(tag)),
                "declared_id": tag.id,
                "fields_known": known,
                "flags": int(tag.RE_FLAGS),
                "id": tag_id,
                "pattern": tag.pattern,
                "scope": _scope_record(tag.scope),
                "slug": tag.slug,
            }
        )
    return entries


def _slot_overrides(
    mapping: Mapping[str, list[Processor]],
) -> dict[str, list[dict[str, Any]]]:
    """Serialize slots whose callbacks differ from the built-in list."""
    defaults = models.Transactions.DEFAULT_PROCESSORS
    result: dict[str, list[dict[str, Any]]] = {}
    for slot in sorted(mapping):
        callbacks = mapping[slot]
        if _same_processors(callbacks, defaults.get(slot, [])):
            continue
        result[slot] = [
            _processor_entry(processor, slot, index)
            for index, processor in enumerate(callbacks)
        ]
    return result


def _same_processors(
    actual: Sequence[Processor], expected: Sequence[Processor]
) -> bool:
    """Return whether both lists contain identical objects in order."""
    return len(actual) == len(expected) and all(
        left is right for left, right in zip(actual, expected, strict=True)
    )


def _processor_entry(
    processor: Processor, slot: str, index: int
) -> dict[str, Any]:
    """Serialize one processor dependency."""
    module, qualname, reference = _callable_identity(processor)
    target, spec = _callable_target(processor)
    fields, known = _processor_fields(processor)
    return {
        "affected_fields": list(fields),
        "class": {
            "importable": "<locals>" not in qualname,
            "module": module,
            "qualname": qualname,
        },
        "closure": _closure_values(target),
        "digest": _callable_digest(processor),
        "fields_known": known,
        "index": index,
        "partial": spec,
        "reference": reference,
        "slot": slot,
    }


def _tag_fields(tag: tags_module.Tag) -> tuple[tuple[str, ...], bool]:
    """Return a tag's output fields and whether they are declared exactly."""
    declared = getattr(tag, "affected_fields", None)
    if declared:
        return tuple(str(field) for field in declared), True
    known = _TAG_FIELDS.get(tag.slug)
    if known:
        return known, True
    return tuple(tag.re.groupindex), True


def _processor_fields(
    processor: Processor,
) -> tuple[tuple[str, ...], bool]:
    """Return the fields a processor can change and whether they are known."""
    target, _ = _callable_target(processor)
    declared = getattr(processor, "affected_fields", None) or getattr(
        target, "affected_fields", None
    )
    if declared:
        return tuple(str(field) for field in declared), True
    known = _PROCESSOR_FIELDS.get(_callable_identity(processor)[2])
    if known:
        return known, True
    keys = _closure_values(target).get("keys")
    if isinstance(keys, tuple) and all(isinstance(key, str) for key in keys):
        return keys, True
    return ("*",), False


def _callable_target(
    processor: Processor,
) -> tuple[object, dict[str, Any] | None]:
    """Unwrap a partial into its target and a portable partial description."""
    if not isinstance(processor, functools.partial):
        return processor, None
    reproducible = all(_safe_value(value) for value in processor.args) and all(
        _safe_value(value) for value in processor.keywords.values()
    )
    return processor.func, {
        "args": list(processor.args) if reproducible else [],
        "keywords": dict(processor.keywords) if reproducible else {},
        "reproducible": reproducible,
    }


def _callable_identity(
    processor: Processor,
) -> tuple[str, str, str]:
    """Return module, qualified name and display reference for a callable."""
    target, _ = _callable_target(processor)
    module = str(getattr(target, "__module__", None) or "<unknown>")
    qualname = str(
        getattr(target, "__qualname__", None)
        or getattr(target, "__name__", None)
        or type(target).__qualname__
    )
    return module, qualname, f"{module}.{qualname}"


def _callable_digest(processor: Processor) -> str:
    """Hash a callable's source, closure values and bound defaults."""
    target, spec = _callable_target(processor)
    digest = hashlib.sha256()
    try:
        source: str | None = inspect.getsource(target)  # type: ignore[arg-type]
    except (OSError, TypeError):
        source = None
    if source is not None:
        normalized = "\n".join(
            line.rstrip() for line in source.strip().splitlines()
        )
        digest.update(f"source\0{normalized}".encode())
    digest.update(f"closure\0{_closure_values(target)!r}".encode())
    digest.update(
        f"defaults\0{getattr(target, '__defaults__', None)!r}".encode()
    )
    if spec is not None:
        digest.update(f"partial\0{spec!r}".encode())
    return f"sha256:{digest.hexdigest()}"


def _closure_values(target: object) -> dict[str, Any]:
    """Return JSON-safe closure values used by a callable."""
    if not callable(target) or getattr(target, "__code__", None) is None:
        return {}
    values = inspect.getclosurevars(target).nonlocals
    return {
        name: value for name, value in values.items() if _safe_value(value)
    }


def _safe_value(value: object) -> bool:
    """Return whether a value survives a JSON round trip unchanged."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_safe_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _safe_value(item)
            for key, item in value.items()
        )
    return False


def _tag_mismatch(
    tag: tags_module.Tag, entry: Mapping[str, Any]
) -> str | None:
    """Return why an imported tag differs from its manifest entry."""
    for attribute, recorded in (
        ("pattern", entry["pattern"]),
        ("slug", entry["slug"]),
        ("id", entry["declared_id"]),
    ):
        if getattr(tag, attribute) != recorded:
            return f"binding {attribute} differs from the manifest"
    if int(tag.RE_FLAGS) != entry["flags"]:
        return "binding regex flags differ from the manifest"
    return None


def _apply_partial(
    candidate: object, spec: Mapping[str, Any] | None
) -> Processor | None:
    """Recreate a recorded partial or return the callable unchanged."""
    processor = cast("Processor", candidate)
    if spec is None:
        return processor
    if not spec["reproducible"]:
        return None
    return functools.partial(processor, *spec["args"], **spec["keywords"])


def _find_default_processor(
    entry: Mapping[str, Any],
) -> Processor | None:
    """Find an identical built-in callback by reference and digest."""
    for callbacks in models.Transactions.DEFAULT_PROCESSORS.values():
        for candidate in callbacks:
            if (
                _callable_identity(candidate)[2] == entry["reference"]
                and _callable_digest(candidate) == entry["digest"]
            ):
                return candidate
    return None


def _lookup(
    mapping: Mapping[str, object] | None, keys: Iterable[str]
) -> object | None:
    """Return the first present explicit binding."""
    if mapping is None:
        return None
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _scope_record(scope: type[object]) -> dict[str, Any]:
    """Serialize a scope marker by stable name or class reference."""
    for name, marker in _SCOPES.items():
        if scope is marker:
            return {"kind": name}
    return {"kind": "custom", "class": _class_reference(scope)}


def _record_options(options: Options) -> dict[str, Any]:
    """Serialize an options instance including inherited fields."""
    return {
        "class": _class_reference(type(options)),
        "values": {
            name: bool(getattr(options, name))
            for name in type(options).names()
        },
    }


def _resolve_options(record: Mapping[str, Any]) -> Options:
    """Rebuild options and reject unknown or non-boolean fields."""
    reference = record["class"]
    if (
        reference["module"] == Options.__module__
        and reference["qualname"] == Options.__qualname__
    ):
        cls: type[Options] = Options
    else:
        imported = _import_object(reference["module"], reference["qualname"])
        if not (isinstance(imported, type) and issubclass(imported, Options)):
            message = "options class must be an Options subclass"
            raise TypeError(message)
        cls = imported
    names = set(cls.names())
    values = record["values"]
    missing = sorted(names - set(values))
    extra = sorted(set(values) - names)
    if missing or extra:
        message = f"option fields differ: missing={missing}, extra={extra}"
        raise ValueError(message)
    if not all(isinstance(value, bool) for value in values.values()):
        message = "option values must be booleans"
        raise TypeError(message)
    return cls(**values)


def _class_reference(cls: object) -> dict[str, Any]:
    """Describe a class by importable module and qualified name."""
    module = str(getattr(cls, "__module__", None) or "<unknown>")
    qualname = str(
        getattr(cls, "__qualname__", None)
        or getattr(cls, "__name__", None)
        or type(cls).__qualname__
    )
    return {
        "importable": "<locals>" not in qualname,
        "module": module,
        "qualname": qualname,
    }


def _digest(payload: Mapping[str, Any]) -> str:
    """Hash a JSON-compatible payload canonically."""
    text = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _sorted_ids(mapping: Mapping[int | str, object]) -> list[int | str]:
    """Return tag IDs in a deterministic integer-before-string order."""
    return sorted(mapping, key=lambda key: (isinstance(key, str), str(key)))


def _version() -> str:
    """Return the package version without creating an import cycle."""
    from . import __version__

    return __version__


def _import_object(module_name: str, qualname: str) -> object:
    """Import an object by module and qualified name."""
    if "<locals>" in qualname:
        message = f"{module_name}.{qualname} is not importable"
        raise ImportError(message)
    target: object = importlib.import_module(module_name)
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def _validate_bank(bank: object) -> str:
    """Validate a bank namespace used in rule identifiers."""
    if not isinstance(bank, str):
        message = "bank must be a string"
        raise TypeError(message)
    if (
        not bank
        or any(char in bank for char in ":/\\")
        or any(char.isspace() for char in bank)
    ):
        message = (
            'bank must be non-empty and must not contain ":", "/", "\\" '
            "or whitespace"
        )
        raise ValueError(message)
    return bank


def _bank(manifest: Mapping[str, Any]) -> str:
    """Return a manifest's validated bank name."""
    return _validate_bank(manifest.get("bank"))


def _removed_tags(removed: Iterable[object]) -> list[int | str]:
    """Validate explicitly removed built-in tag IDs."""
    available = set(models.Transactions.default_tags())
    result: list[int | str] = []
    for tag_id in removed:
        if not isinstance(tag_id, (int, str)):
            message = "removed tag ids must be integers or strings"
            raise TypeError(message)
        if tag_id not in available:
            message = f"removed tag {tag_id!r} is not a built-in tag"
            raise ValueError(message)
        result.append(tag_id)
    return sorted(set(result), key=str)


def _removed_slots(removed: Iterable[object]) -> list[str]:
    """Validate explicitly removed built-in processor slots."""
    available = set(models.Transactions.DEFAULT_PROCESSORS)
    result: list[str] = []
    for slot in removed:
        if not isinstance(slot, str):
            message = "removed processor slots must be strings"
            raise TypeError(message)
        if slot not in available:
            message = f"removed processor slot {slot!r} is not built in"
            raise ValueError(message)
        result.append(slot)
    return sorted(set(result))


def _all_fields(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Union affected fields across every recorded tag and processor."""
    fields: list[str] = []
    for entry in manifest["tag_overrides"]:
        fields.extend(entry["affected_fields"])
    for entries in manifest["processor_slots"].values():
        for entry in entries:
            fields.extend(entry["affected_fields"])
    return tuple(dict.fromkeys(fields)) or ("*",)


def _validate_manifest(manifest: object) -> None:
    """Validate the manifest's top-level and nested field types.

    Raises:
        TypeError: The manifest or a field has the wrong type.
        ValueError: The schema, option name or scope kind is invalid.
    """
    if not isinstance(manifest, Mapping):
        message = "manifest must be a mapping"
        raise TypeError(message)
    if manifest.get("schema") != SCHEMA:
        message = f"unsupported manifest schema {manifest.get('schema')!r}"
        raise ValueError(message)
    _ = _bank(manifest)
    for key in ("content_version", "mt940_version"):
        if not isinstance(manifest.get(key), str):
            message = f"{key} must be a string"
            raise TypeError(message)
    environment = _require_mapping(manifest, "environment")
    for area in ("tag", "processor"):
        _ = _require_mapping(environment, area)
    options = _require_mapping(manifest, "options")
    _validate_class(options.get("class"), "options.class")
    _ = _require_mapping(options, "values")
    _require_string_list(manifest, "removed_processor_slots")
    _require_string_list(manifest, "transaction_boundary")
    removed = manifest.get("removed_tag_ids")
    if not isinstance(removed, list) or not all(
        isinstance(tag_id, (int, str)) for tag_id in removed
    ):
        message = "removed_tag_ids must be a list of integers or strings"
        raise TypeError(message)
    overrides = manifest.get("tag_overrides")
    if not isinstance(overrides, list):
        message = "tag_overrides must be a list"
        raise TypeError(message)
    for entry in overrides:
        _validate_tag_entry(entry)
    slots = _require_mapping(manifest, "processor_slots")
    for slot, entries in slots.items():
        if not isinstance(entries, list):
            message = f"processor slot {slot!r} must be a list"
            raise TypeError(message)
        for entry in entries:
            _validate_processor_entry(entry)


def _require_mapping(
    mapping: Mapping[str, Any], key: str
) -> Mapping[str, Any]:
    """Require a top-level field to be a mapping."""
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        message = f"{key} must be a mapping"
        raise TypeError(message)
    return value


def _require_string_list(mapping: Mapping[str, Any], key: str) -> None:
    """Require a top-level field to be a list of strings."""
    value = mapping.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        message = f"{key} must be a list of strings"
        raise TypeError(message)


def _validate_class(value: object, label: str) -> None:
    """Validate a serialized module and qualified-name reference."""
    if not isinstance(value, Mapping):
        message = f"{label} must be a mapping"
        raise TypeError(message)
    for key in ("module", "qualname"):
        if not isinstance(value.get(key), str):
            message = f"{label}.{key} must be a string"
            raise TypeError(message)
    if not isinstance(value.get("importable"), bool):
        message = f"{label}.importable must be a boolean"
        raise TypeError(message)


def _validate_tag_entry(entry: object) -> None:
    """Validate one serialized tag override."""
    if not isinstance(entry, Mapping):
        message = "tag_overrides entries must be mappings"
        raise TypeError(message)
    if not isinstance(entry.get("id"), (int, str)):
        message = "tag override id must be an integer or string"
        raise TypeError(message)
    for key in ("pattern", "slug"):
        if not isinstance(entry.get(key), str):
            message = f"tag override {key} must be a string"
            raise TypeError(message)
    if not isinstance(entry.get("flags"), int) or not isinstance(
        entry.get("declared_id"), (int, str)
    ):
        message = "tag override flags and declared_id have wrong types"
        raise TypeError(message)
    _validate_class(entry.get("class"), "tag override class")
    _require_string_list(entry, "affected_fields")
    scope = entry.get("scope")
    if not isinstance(scope, Mapping):
        message = "tag override scope must be a mapping"
        raise TypeError(message)
    if scope.get("kind") not in (*_SCOPES, "custom"):
        message = f"unknown scope kind {scope.get('kind')!r}"
        raise ValueError(message)
    if scope["kind"] == "custom":
        _validate_class(scope.get("class"), "tag override scope.class")


def _validate_processor_entry(entry: object) -> None:
    """Validate one serialized processor dependency."""
    if not isinstance(entry, Mapping):
        message = "processor slot entries must be mappings"
        raise TypeError(message)
    for key in ("reference", "slot"):
        if not isinstance(entry.get(key), str):
            message = f"processor {key} must be a string"
            raise TypeError(message)
    if not isinstance(entry.get("index"), int) or isinstance(
        entry.get("index"), bool
    ):
        message = "processor index must be an integer"
        raise TypeError(message)
    if not isinstance(entry.get("fields_known"), bool):
        message = "processor fields_known must be a boolean"
        raise TypeError(message)
    _validate_class(entry.get("class"), "processor class")
    if not isinstance(entry.get("digest"), str):
        message = "processor digest must be a string"
        raise TypeError(message)
    _require_string_list(entry, "affected_fields")
    _ = _require_mapping(entry, "closure")
    partial = entry.get("partial")
    if partial is not None and (
        not isinstance(partial, Mapping)
        or not isinstance(partial.get("reproducible"), bool)
    ):
        message = "processor partial must have a reproducible boolean"
        raise TypeError(message)
