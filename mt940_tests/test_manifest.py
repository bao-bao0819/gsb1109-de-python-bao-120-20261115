"""Exercise bank manifest export, verification and strict restoration."""

from __future__ import annotations

import functools
import json
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

import mt940
from mt940 import manifest

if TYPE_CHECKING:
    from pathlib import Path

SOURCE = (
    ":20:REF\n"
    ":60F:C260101EUR100,00\n"
    ":61:2601020102C5,00NTRFCOFFEE//BANK\n"
    ":86:Coffee supplies\n"
    ":62F:C260102EUR105,00\n"
)


class Department(mt940.tags.Tag):
    """Read a fictional :99: department code into statement metadata."""

    id: ClassVar[int | str] = 99
    scope: ClassVar[
        type[mt940.models.Transactions | mt940.models.Transaction]
    ] = mt940.models.Transactions
    pattern: ClassVar[str] = r"^(?P<department>[A-Z]{2,8})$"
    affected_fields: ClassVar[tuple[str, ...]] = ("department",)


def add_category(
    transactions: Any,
    tag: Any,
    tag_dict: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Add a category field and declare its impact."""
    del transactions, tag, tag_dict
    return {**result, "category": "supplies"}


add_category.affected_fields = ("category",)  # type: ignore[attr-defined]


class ExtendedOptions(mt940.Options):
    """An importable Options subclass used to test class resolution."""


def test_export_records_configuration_and_content_version() -> None:
    exported = manifest.export_manifest(
        "acme",
        tags={61: mt940.tags.StatementGLS()},
        options=mt940.Options(reversal_sign=True),
        transaction_boundary={"transaction_reference_number"},
    )
    assert exported["schema"] == manifest.SCHEMA
    assert exported["bank"] == "acme"
    assert exported["content_version"] == manifest.content_version(exported)
    assert exported["content_version"].startswith("sha256:")
    assert exported["transaction_boundary"] == ["transaction_reference_number"]
    assert exported["options"]["values"]["reversal_sign"] is True
    assert exported["tag_overrides"][0]["id"] == 61
    assert exported["tag_overrides"][0]["class"]["qualname"] == "StatementGLS"
    assert exported["environment"]["tag"]["61"]["slug"] == "statement"
    assert manifest.verify_manifest(exported) == []


def test_export_omits_untouched_defaults() -> None:
    exported = manifest.export_manifest("acme")
    assert exported["tag_overrides"] == []
    assert exported["processor_slots"] == {}
    assert exported["removed_tag_ids"] == []
    assert exported["removed_processor_slots"] == []


def test_restore_reproduces_same_parse_result() -> None:
    original = mt940.parse(
        SOURCE,
        tags={99: Department()},
        options=mt940.Options(reversal_sign=True),
    )
    captured = manifest.capture_manifest(original, "acme")
    restored = manifest.restore_manifest(captured)
    reparsed = restored.parse(SOURCE)
    assert reparsed.data == original.data
    assert [item.data for item in reparsed] == [item.data for item in original]
    assert restored.options.reversal_sign is True
    assert isinstance(restored.tags[99], Department)


def test_manifest_json_round_trip_is_stable(tmp_path: Path) -> None:
    exported = manifest.export_manifest("acme", options=mt940.Options.all())
    path = tmp_path / "acme.json"
    manifest.write_manifest(exported, path)
    loaded = manifest.read_manifest(path)
    assert loaded == exported
    assert manifest.content_version(loaded) == exported["content_version"]
    text = manifest.manifest_dumps(exported)
    assert json.loads(text) == exported


def test_edited_manifest_is_rejected_before_parsing() -> None:
    exported = manifest.export_manifest("acme")
    exported["options"]["values"]["reversal_sign"] = True
    issues = manifest.verify_manifest(exported)
    assert [issue.kind for issue in issues] == ["content_version"]
    with pytest.raises(manifest.ManifestError) as exc_info:
        manifest.restore_manifest(exported)
    assert "content_version" in str(exc_info.value)
    assert exc_info.value.bank == "acme"


def test_missing_processor_lists_rule_and_affected_fields() -> None:
    original = mt940.parse(
        SOURCE,
        processors={
            "post_transaction_details": [
                *mt940.models.Transactions.DEFAULT_PROCESSORS[
                    "post_transaction_details"
                ],
                add_category,
            ]
        },
    )
    captured = manifest.capture_manifest(original, "acme")
    entry = captured["processor_slots"]["post_transaction_details"][-1]
    entry["class"] = {
        "importable": True,
        "module": "not_an_installed_module",
        "qualname": "add_category",
    }
    entry["reference"] = "not_an_installed_module.add_category"
    captured["content_version"] = manifest.content_version(captured)
    issues = manifest.verify_manifest(captured)
    assert len(issues) == 1
    assert issues[0].rule == "acme:processor:post_transaction_details:1"
    assert issues[0].affected_fields == ("category",)
    with pytest.raises(manifest.ManifestError) as exc_info:
        manifest.restore_manifest(captured)
    message = str(exc_info.value)
    assert "category" in message
    assert "not a built-in default" not in message


def test_missing_processor_never_falls_back_to_default() -> None:
    captured = manifest.export_manifest(
        "acme",
        processors={
            "post_transaction_details": [
                *mt940.models.Transactions.DEFAULT_PROCESSORS[
                    "post_transaction_details"
                ],
                add_category,
            ]
        },
    )
    entry = captured["processor_slots"]["post_transaction_details"][-1]
    entry["class"] = {
        "importable": True,
        "module": "not_an_installed_module",
        "qualname": "add_category",
    }
    captured["content_version"] = manifest.content_version(captured)
    with pytest.raises(manifest.ManifestError):
        manifest.restore_manifest(captured)


def test_unimportable_tag_requires_an_explicit_binding() -> None:
    def factory() -> mt940.tags.Tag:
        class Local(mt940.tags.Tag):
            id: ClassVar[int | str] = 98
            scope: ClassVar[
                type[mt940.models.Transactions | mt940.models.Transaction]
            ] = mt940.models.Transactions
            pattern: ClassVar[str] = r"^(?P<note>[A-Z]+)$"

        return Local()

    tag = factory()
    exported = manifest.export_manifest("acme", tags={98: tag})
    entry = exported["tag_overrides"][0]
    assert entry["class"]["importable"] is False
    issues = manifest.verify_manifest(exported)
    assert issues[0].kind == "tag"
    assert issues[0].affected_fields == ("note",)
    with pytest.raises(manifest.ManifestError):
        manifest.restore_manifest(exported)
    restored = manifest.restore_manifest(
        exported, available_tags={"acme:tag:98": tag}
    )
    assert restored.tags[98] is tag


def test_unimportable_tag_without_binding_is_reported() -> None:
    def factory() -> mt940.tags.Tag:
        class Local(Department):
            pattern: ClassVar[str] = r"^(?P<department>[a-z]{2,8})$"

        return Local()

    exported = manifest.export_manifest("acme", tags={99: factory()})
    issues = manifest.verify_manifest(exported)
    assert issues[0].kind == "tag"
    assert issues[0].affected_fields == ("department",)
    with pytest.raises(manifest.ManifestError) as exc_info:
        manifest.restore_manifest(exported)
    assert "not importable" in str(exc_info.value)


def test_explicit_binding_allows_unimportable_rule() -> None:
    exported = manifest.export_manifest("acme", tags={99: Department()})
    entry = exported["tag_overrides"][0]
    entry["class"] = {
        "importable": True,
        "module": "not_an_installed_module",
        "qualname": "Department",
    }
    exported["content_version"] = manifest.content_version(exported)
    restored = manifest.restore_manifest(
        exported, available_tags={"acme:tag:99": Department}
    )
    assert isinstance(restored.tags[99], Department)


def test_registry_isolates_same_named_rules_per_bank() -> None:
    registry = manifest.ManifestRegistry()
    registry.add(
        manifest.export_manifest(
            "bank-a", tags={61: mt940.tags.StatementGLS()}
        )
    )
    registry.add(
        manifest.export_manifest(
            "bank-b", tags={61: mt940.tags.StatementASNB()}
        )
    )
    assert registry.banks == ("bank-a", "bank-b")
    a = registry.restore("bank-a")
    b = registry.restore("bank-b")
    assert type(a.tags[61]) is mt940.tags.StatementGLS
    assert type(b.tags[61]) is mt940.tags.StatementASNB
    assert a.content_version != b.content_version


def test_registry_rejects_conflicting_version_for_same_bank() -> None:
    registry = manifest.ManifestRegistry()
    registry.add(manifest.export_manifest("acme"))
    with pytest.raises(manifest.ManifestError) as exc_info:
        registry.add(
            manifest.export_manifest("acme", options=mt940.Options.all())
        )
    assert exc_info.value.issues[0].kind == "bank"
    registry.add(
        manifest.export_manifest("acme", options=mt940.Options.all()),
        replace=True,
    )
    assert registry.restore("acme").options.reversal_sign is True


def test_bank_namespace_rejects_separators() -> None:
    for bank in ("", "a:b", "a/b", "a b"):
        with pytest.raises(ValueError, match="bank"):
            manifest.export_manifest(bank)
    with pytest.raises(TypeError, match="bank"):
        manifest.export_manifest(1)  # type: ignore[arg-type]


def test_removed_defaults_are_not_silently_restored() -> None:
    exported = manifest.export_manifest(
        "acme", removed_tags=[13], removed_processor_slots=["pre_statement"]
    )
    assert exported["removed_tag_ids"] == [13]
    assert exported["removed_processor_slots"] == ["pre_statement"]
    restored = manifest.restore_manifest(exported)
    assert 13 not in restored.tags
    assert restored.processors["pre_statement"] == []


def test_removal_argument_must_name_a_builtin() -> None:
    with pytest.raises(ValueError, match="not a built-in tag"):
        manifest.export_manifest("acme", removed_tags=[123])
    with pytest.raises(ValueError, match="is not built in"):
        manifest.export_manifest("acme", removed_processor_slots=["nope"])


def test_tampered_tag_pattern_is_detected() -> None:
    exported = manifest.export_manifest("acme", tags={99: Department()})
    entry = exported["tag_overrides"][0]
    entry["class"] = {
        "importable": True,
        "module": "mt940.tags",
        "qualname": "Statement",
    }
    entry["pattern"] = r"^(?P<department>.+)$"
    exported["content_version"] = manifest.content_version(exported)
    issues = manifest.verify_manifest(exported)
    assert issues[0].kind == "tag"
    assert "pattern" in issues[0].reason


def test_environment_change_is_reported_with_fields() -> None:
    exported = manifest.export_manifest("acme")
    exported["environment"]["tag"]["61"]["digest"] = "sha256:changed"
    exported["content_version"] = manifest.content_version(exported)
    issues = manifest.verify_manifest(exported)
    assert issues[0].kind == "environment"
    assert issues[0].rule == "acme:tag:61"
    assert "amount" in issues[0].affected_fields


def test_options_with_unknown_field_is_rejected() -> None:
    exported = manifest.export_manifest("acme")
    exported["options"]["values"]["future_switch"] = True
    exported["content_version"] = manifest.content_version(exported)
    issues = manifest.verify_manifest(exported)
    assert issues[0].kind == "options"


def test_schema_and_field_types_are_validated() -> None:
    exported = manifest.export_manifest("acme")
    exported["schema"] = "other/1"
    with pytest.raises(ValueError, match="schema"):
        manifest.verify_manifest(exported)
    with pytest.raises(TypeError, match="mapping"):
        manifest.verify_manifest([])  # type: ignore[arg-type]


def test_parse_statements_uses_restored_configuration() -> None:
    config = manifest.restore_manifest(manifest.export_manifest("acme"))
    source = ":20:ONE\n:61:260102C1,00NTRFREF\n:20:TWO\n:61:260103C2,00NTRFREF"
    statements = config.parse_statements(source)
    assert len(statements) == 2
    assert [len(item) for item in statements] == [1, 1]


def test_default_closure_dependency_is_rebuilt() -> None:
    callback = mt940.processors.transactions_to_transaction(
        "transaction_reference"
    )
    exported = manifest.export_manifest(
        "acme",
        processors={
            "post_statement": [
                *mt940.models.Transactions.DEFAULT_PROCESSORS[
                    "post_statement"
                ],
                callback,
            ]
        },
    )
    assert manifest.verify_manifest(exported) == []
    restored = manifest.restore_manifest(exported)
    parsed = restored.parse(":20:REF\n:61:260102C1,00NTRFREF\n")
    assert parsed[0].data["transaction_reference"] == "REF"


def test_partial_dependency_round_trips() -> None:
    callback = mt940.processors.transaction_details_post_processor_with_space
    exported = manifest.export_manifest(
        "acme", processors={"post_transaction_details": [callback]}
    )
    restored = manifest.restore_manifest(exported)
    processor = restored.processors["post_transaction_details"][0]
    assert isinstance(processor, functools.partial)
    assert (
        processor.func is mt940.processors.transaction_details_post_processor
    )
    assert processor.keywords == {"space": True}


def test_registry_accessors_and_missing_bank() -> None:
    registry = manifest.ManifestRegistry()
    exported = manifest.export_manifest("acme")
    registry.add(exported)
    assert registry.get("acme") == exported
    assert registry.verify("acme") == []
    with pytest.raises(KeyError):
        registry.get("absent")
    with pytest.raises(KeyError):
        registry.verify("absent")
    with pytest.raises(KeyError):
        registry.restore("absent")


def test_non_object_json_is_rejected() -> None:
    with pytest.raises(TypeError, match="object"):
        manifest.manifest_loads("[]")


def test_environment_processor_change_reports_processor_fields() -> None:
    exported = manifest.export_manifest("acme")
    exported["environment"]["processor"]["pre_statement"][0]["digest"] = "x"
    exported["content_version"] = manifest.content_version(exported)
    issues = manifest.verify_manifest(exported)
    assert issues[0].rule == "acme:processor:pre_statement"
    assert issues[0].affected_fields == ("day",)


def test_environment_tag_without_fields_reports_star() -> None:
    exported = manifest.export_manifest("acme")
    exported["environment"]["tag"]["61"].pop("affected_fields")
    exported["content_version"] = manifest.content_version(exported)
    issues = manifest.verify_manifest(exported)
    assert issues[0].affected_fields == ("*",)


def test_bound_non_tag_is_reported() -> None:
    exported = manifest.export_manifest("acme", tags={99: Department()})
    entry = exported["tag_overrides"][0]
    entry["class"] = {
        "importable": True,
        "module": "nowhere",
        "qualname": "Unknown",
    }
    exported["content_version"] = manifest.content_version(exported)
    issues = manifest.verify_manifest(
        exported, available_tags={"acme:tag:99": object}
    )
    assert issues[0].reason == "binding is not a tag instance or class"


def test_bound_tag_shape_mismatch_is_reported() -> None:
    exported = manifest.export_manifest("acme", tags={99: Department()})
    exported["tag_overrides"][0]["pattern"] = r"^(?P<department>[0-9]+)$"
    exported["content_version"] = manifest.content_version(exported)
    issues = manifest.verify_manifest(
        exported, available_tags={"acme:tag:99": Department}
    )
    assert "pattern" in issues[0].reason


def test_non_callable_processor_binding_is_reported() -> None:
    callback = add_category
    exported = manifest.export_manifest(
        "acme", processors={"post_transaction_details": [callback]}
    )
    issues = manifest.verify_manifest(
        exported,
        available_processors={"acme:processor:post_transaction_details:0": 1},
    )
    assert issues[0].reason == "binding is not callable"


def test_non_serializable_partial_is_reported() -> None:
    callback = functools.partial(add_category, object())
    exported = manifest.export_manifest(
        "acme", processors={"post_transaction_details": [callback]}
    )
    entry = exported["processor_slots"]["post_transaction_details"][0]
    assert entry["partial"]["reproducible"] is False
    issues = manifest.verify_manifest(
        exported,
        available_processors={
            "acme:processor:post_transaction_details:0": add_category
        },
    )
    assert "partial arguments" in issues[0].reason


def test_changed_processor_binding_is_reported() -> None:
    exported = manifest.export_manifest(
        "acme", processors={"post_transaction_details": [add_category]}
    )

    def other(
        transactions: Any,
        tag: Any,
        tag_dict: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a different result so the digest cannot match."""
        del transactions, tag, tag_dict
        return {**result, "category": "other"}

    assert other({}, {}, {}, {"category": "x"})["category"] == "other"

    issues = manifest.verify_manifest(
        exported,
        available_processors={
            "acme:processor:post_transaction_details:0": other
        },
    )
    assert "digest" in issues[0].reason


def test_unknown_option_scope_and_removal_types_are_rejected() -> None:
    exported = manifest.export_manifest("acme")
    bad_scope = manifest.manifest_loads(manifest.manifest_dumps(exported))
    bad_scope["tag_overrides"] = [
        {
            **manifest.export_manifest("acme", tags={99: Department()})[
                "tag_overrides"
            ][0],
            "scope": {"kind": "nowhere"},
        }
    ]
    with pytest.raises(ValueError, match="scope kind"):
        manifest.verify_manifest(bad_scope)
    with pytest.raises(TypeError, match="removed tag ids"):
        manifest.export_manifest("acme", removed_tags=[1.5])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="removed processor slots"):
        manifest.export_manifest("acme", removed_processor_slots=[1])  # type: ignore[list-item]


def test_manifest_field_type_validation() -> None:
    exported = manifest.export_manifest("acme")
    cases: list[tuple[str, object, str]] = [
        ("content_version", 1, "must be a string"),
        ("environment", [], "must be a mapping"),
        ("options", [], "must be a mapping"),
        ("removed_processor_slots", [1], "list of strings"),
        ("transaction_boundary", [1], "list of strings"),
        ("removed_tag_ids", [1.5], "integers or strings"),
        ("tag_overrides", {}, "must be a list"),
        ("processor_slots", [], "must be a mapping"),
    ]
    for key, value, message in cases:
        broken = manifest.manifest_loads(manifest.manifest_dumps(exported))
        broken[key] = value
        with pytest.raises(TypeError, match=message):
            manifest.verify_manifest(broken)


def test_nested_entry_type_validation() -> None:
    exported = manifest.export_manifest("acme", tags={99: Department()})
    tag_cases: list[tuple[str, object, str]] = [
        ("id", 1.5, "id must be an integer"),
        ("pattern", 1, "pattern must be a string"),
        ("flags", "x", "flags and declared_id"),
        ("class", [], "class must be a mapping"),
        ("scope", [], "scope must be a mapping"),
    ]
    for key, value, message in tag_cases:
        broken = manifest.manifest_loads(manifest.manifest_dumps(exported))
        broken["tag_overrides"][0][key] = value
        with pytest.raises(TypeError, match=message):
            manifest.verify_manifest(broken)
    broken = manifest.manifest_loads(manifest.manifest_dumps(exported))
    broken["tag_overrides"] = [1]
    with pytest.raises(TypeError, match="entries must be mappings"):
        manifest.verify_manifest(broken)


def test_processor_entry_type_validation() -> None:
    exported = manifest.export_manifest(
        "acme", processors={"post_transaction_details": [add_category]}
    )
    cases: list[tuple[str, object, str]] = [
        ("reference", 1, "reference must be a string"),
        ("index", "x", "index must be an integer"),
        ("fields_known", "x", "fields_known must be a boolean"),
        ("digest", 1, "digest must be a string"),
        ("class", [], "class must be a mapping"),
        ("closure", [], "closure must be a mapping"),
        ("partial", {"reproducible": "x"}, "reproducible boolean"),
    ]
    for key, value, message in cases:
        broken = manifest.manifest_loads(manifest.manifest_dumps(exported))
        broken["processor_slots"]["post_transaction_details"][0][key] = value
        with pytest.raises(TypeError, match=message):
            manifest.verify_manifest(broken)
    broken = manifest.manifest_loads(manifest.manifest_dumps(exported))
    broken["processor_slots"] = {"post_transaction_details": {}}
    with pytest.raises(TypeError, match="must be a list"):
        manifest.verify_manifest(broken)
    broken = manifest.manifest_loads(manifest.manifest_dumps(exported))
    broken["processor_slots"] = {"post_transaction_details": [1]}
    with pytest.raises(TypeError, match="entries must be mappings"):
        manifest.verify_manifest(broken)


def test_class_reference_shape_validation() -> None:
    exported = manifest.export_manifest("acme")
    for key, value in (("module", 1), ("qualname", 1), ("importable", "x")):
        broken = manifest.manifest_loads(manifest.manifest_dumps(exported))
        broken["options"]["class"][key] = value
        with pytest.raises(TypeError):
            manifest.verify_manifest(broken)


def test_options_class_must_subclass_options() -> None:
    exported = manifest.export_manifest("acme")
    exported["options"]["class"] = {
        "importable": True,
        "module": "mt940.tags",
        "qualname": "Tag",
    }
    exported["content_version"] = manifest.content_version(exported)
    issues = manifest.verify_manifest(exported)
    assert issues[0].kind == "options"


def test_options_values_must_be_booleans() -> None:
    exported = manifest.export_manifest("acme")
    exported["options"]["values"]["reversal_sign"] = 1
    exported["content_version"] = manifest.content_version(exported)
    issues = manifest.verify_manifest(exported)
    assert "booleans" in issues[0].reason


def test_custom_scope_class_is_recorded() -> None:
    class StatementScope(mt940.models.Transactions):
        """A custom statement scope subclass."""

    class ScopedDepartment(Department):
        scope: ClassVar[
            type[mt940.models.Transactions | mt940.models.Transaction]
        ] = StatementScope

    tag = ScopedDepartment()
    exported = manifest.export_manifest("acme", tags={99: tag})
    scope = exported["tag_overrides"][0]["scope"]
    assert scope["kind"] == "custom"
    assert scope["class"]["qualname"].endswith("StatementScope")
    assert scope["class"]["importable"] is False
    assert (
        manifest.verify_manifest(exported, available_tags={"acme:tag:99": tag})
        == []
    )


def test_tag_without_declared_fields_uses_regex_groups() -> None:
    class Undeclared(mt940.tags.Tag):
        id: ClassVar[int | str] = 97
        scope: ClassVar[
            type[mt940.models.Transactions | mt940.models.Transaction]
        ] = mt940.models.Transactions
        pattern: ClassVar[str] = r"^(?P<code>[A-Z]+)$"

    exported = manifest.export_manifest("acme", tags={97: Undeclared()})
    entry = exported["tag_overrides"][0]
    assert entry["affected_fields"] == ["code"]
    assert entry["fields_known"] is True


def test_undeclared_processor_reports_star() -> None:
    def unknown(
        transactions: Any,
        tag: Any,
        tag_dict: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the result unchanged without declaring affected fields."""
        del transactions, tag, tag_dict
        return result

    assert unknown({}, {}, {}, {"a": 1}) == {"a": 1}

    exported = manifest.export_manifest(
        "acme", processors={"post_transaction_details": [unknown]}
    )
    entry = exported["processor_slots"]["post_transaction_details"][0]
    assert entry["affected_fields"] == ["*"]
    assert entry["fields_known"] is False


def test_callable_without_source_still_digests() -> None:
    namespace: dict[str, Any] = {}
    exec(
        "def dynamic(transactions, tag, tag_dict, result):\n"
        "    return result\n",
        namespace,
    )
    dynamic = namespace["dynamic"]
    exported = manifest.export_manifest(
        "acme", processors={"post_transaction_details": [dynamic]}
    )
    entry = exported["processor_slots"]["post_transaction_details"][0]
    assert entry["digest"].startswith("sha256:")
    restored = manifest.restore_manifest(
        exported,
        available_processors={
            "acme:processor:post_transaction_details:0": dynamic
        },
    )
    assert restored.processors["post_transaction_details"][0] is dynamic


def test_closure_without_code_has_no_values() -> None:
    exported = manifest.export_manifest(
        "acme",
        processors={
            "post_transaction_details": [
                functools.partial(
                    mt940.processors.transaction_details_post_processor,
                    space=True,
                )
            ]
        },
    )
    entry = exported["processor_slots"]["post_transaction_details"][0]
    assert entry["closure"] == {}


def test_tag_override_with_removed_tag_id_is_dropped() -> None:
    exported = manifest.export_manifest(
        "acme",
        tags={99: Department()},
        removed_tags=[99]
        if 99 in mt940.models.Transactions.default_tags()
        else [],
    )
    restored = manifest.restore_manifest(exported)
    assert isinstance(restored.tags[99], Department)


def test_partial_missing_arguments_is_reported() -> None:
    exported = manifest.export_manifest(
        "acme", processors={"post_transaction_details": [add_category]}
    )
    entry = exported["processor_slots"]["post_transaction_details"][0]
    entry["partial"] = {"args": [], "keywords": {}, "reproducible": True}
    entry["digest"] = manifest._callable_digest(
        functools.partial(add_category)
    )
    exported["content_version"] = manifest.content_version(exported)
    assert manifest.verify_manifest(exported) == []


def test_imported_tag_is_matched_by_qualname() -> None:
    exported = manifest.export_manifest(
        "acme", tags={61: mt940.tags.StatementGLS()}
    )
    issues = manifest.verify_manifest(
        exported,
        available_tags={"mt940.tags.StatementGLS": mt940.tags.StatementGLS},
    )
    assert issues == []


def test_unbound_lookup_misses_for_all_keys() -> None:
    exported = manifest.export_manifest("acme")
    issues = manifest.verify_manifest(
        exported, available_tags={"unrelated": mt940.tags.Tag}
    )
    assert issues == []


def test_all_fields_collects_tag_and_processor_entries() -> None:
    exported = manifest.export_manifest(
        "acme",
        tags={99: Department()},
        processors={"post_transaction_details": [add_category]},
        options=mt940.Options(),
    )
    exported["options"]["values"]["reversal_sign"] = True
    issues = manifest.verify_manifest(exported)
    assert issues[0].kind == "content_version"
    assert "department" in issues[0].affected_fields
    assert "category" in issues[0].affected_fields


def test_closure_values_skip_unserializable_values() -> None:
    marker = object()

    def with_marker(
        transactions: Any,
        tag: Any,
        tag_dict: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Capture an object that must not enter the manifest."""
        del transactions, tag, tag_dict
        return result if marker is not None else {}

    assert with_marker({}, {}, {}, {"a": 1}) == {"a": 1}

    exported = manifest.export_manifest(
        "acme", processors={"post_transaction_details": [with_marker]}
    )
    entry = exported["processor_slots"]["post_transaction_details"][0]
    assert entry["closure"] == {}


def test_imported_options_subclass_is_accepted() -> None:
    exported = manifest.export_manifest("acme")
    exported["options"]["class"] = {
        "importable": True,
        "module": "mt940.options",
        "qualname": "Options",
    }
    exported["content_version"] = manifest.content_version(exported)
    assert manifest.verify_manifest(exported) == []


def test_regex_flags_mismatch_is_reported() -> None:
    exported = manifest.export_manifest("acme", tags={99: Department()})
    exported["tag_overrides"][0]["flags"] = 0
    exported["content_version"] = manifest.content_version(exported)
    issues = manifest.verify_manifest(
        exported, available_tags={"acme:tag:99": Department}
    )
    assert "flags" in issues[0].reason


def test_removed_slots_are_sorted_and_deduplicated() -> None:
    exported = manifest.export_manifest(
        "acme",
        removed_processor_slots=["pre_statement", "pre_statement"],
        removed_tags=[13, 13],
    )
    assert exported["removed_processor_slots"] == ["pre_statement"]
    assert exported["removed_tag_ids"] == [13]


def test_capture_manifest_records_removed_slots() -> None:
    statement = mt940.parse(
        ":20:REF\n:61:260102C1,00NTRFREF\n",
        processors={"pre_statement": []},
    )
    captured = manifest.capture_manifest(statement, "acme")
    assert captured["processor_slots"]["pre_statement"] == []
    restored = manifest.restore_manifest(captured)
    assert restored.processors["pre_statement"] == []


def test_missing_builtin_tag_is_reported() -> None:
    exported = manifest.export_manifest("acme")
    exported["tag_ids"] = []
    exported["removed_tag_ids"] = [13]
    exported["content_version"] = manifest.content_version(exported)
    assert manifest.verify_manifest(exported) == []


def test_custom_options_subclass_is_resolved() -> None:
    exported = manifest.export_manifest(
        "acme", options=ExtendedOptions(reversal_sign=True)
    )
    assert exported["options"]["class"]["qualname"] == "ExtendedOptions"
    restored = manifest.restore_manifest(exported)
    assert isinstance(restored.options, ExtendedOptions)
    assert restored.options.reversal_sign is True


def test_lookup_with_unrelated_binding_falls_through_to_import() -> None:
    exported = manifest.export_manifest("acme", tags={99: Department()})
    issues = manifest.verify_manifest(
        exported, available_tags={"unrelated-key": object}
    )
    assert issues == []


def test_callable_object_without_code_has_no_closure() -> None:
    class CallableObject:
        """A processor-like object without a ``__code__`` attribute."""

        affected_fields = ("transaction_details",)

        def __call__(
            self,
            transactions: Any,
            tag: Any,
            tag_dict: dict[str, Any],
            result: dict[str, Any],
        ) -> dict[str, Any]:
            """Return the result unchanged."""
            del transactions, tag, tag_dict
            return result

    assert CallableObject()({}, {}, {}, {"a": 1}) == {"a": 1}
    exported = manifest.export_manifest(
        "acme", processors={"post_transaction_details": [CallableObject()]}
    )
    entry = exported["processor_slots"]["post_transaction_details"][0]
    assert entry["closure"] == {}


def test_closure_with_dict_value_is_marked_unsafe() -> None:
    mapping = {"key": object()}

    def with_mapping(
        transactions: Any,
        tag: Any,
        tag_dict: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Capture a dictionary in the closure."""
        del transactions, tag, tag_dict
        return {**result, **mapping}

    assert with_mapping({}, {}, {}, {}) == {"key": mapping["key"]}
    exported = manifest.export_manifest(
        "acme", processors={"post_transaction_details": [with_mapping]}
    )
    entry = exported["processor_slots"]["post_transaction_details"][0]
    assert entry["closure"] == {}


def test_bindings_do_not_leak_between_banks() -> None:
    tag = Department()
    a = manifest.export_manifest("bank-a", tags={99: tag})
    b = manifest.export_manifest("bank-b", tags={99: tag})
    for exported in (a, b):
        exported["tag_overrides"][0]["class"] = {
            "importable": True,
            "module": "not_installed",
            "qualname": "Department",
        }
        exported["content_version"] = manifest.content_version(exported)
    # Only bank-a's rule is bound; bank-b must still fail rather than reuse it.
    restored = manifest.restore_manifest(
        a, available_tags={"bank-a:tag:99": tag}
    )
    assert restored.bank == "bank-a"
    with pytest.raises(manifest.ManifestError):
        manifest.restore_manifest(b, available_tags={"bank-a:tag:99": tag})
