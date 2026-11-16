#
# Copyright (c) 2012-2018, CRS4
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""
Schema dependency manifests.

A manifest describes everything that is needed to assemble and validate a batch
of HL7 messages with HL7apy on another machine:

* the HL7apy, Python and HL7 versions;
* the message profile artifact and its semantic schema digest;
* every profile extension (a non-standard element or a datatype override) with
  the dependency chain that must be present;
* the validation options (validation level, group discovery, forced validation
  and encoding chars).

The manifest is built from schemas only. It never contains message values,
segment text or any other patient data. Verification is intentionally strict:
when a profile or one of its extensions is missing, a dependency chain is
raised instead of silently using the standard HL7 schema.

The module can also be used from the command line::

    python -m hl7apy.manifest build tests/profiles/iti_21 --version 2.5 -o manifest.json
    python -m hl7apy.manifest verify manifest.json --profiles-dir tests/profiles
"""

import argparse
import functools
import hashlib
import json
import os
import platform
import pickletools
import re
import shutil
import sys
from collections.abc import Mapping

import hl7apy
from hl7apy import (
    SUPPORTED_LIBRARIES,
    check_encoding_chars,
    check_validation_level,
    check_version,
    get_default_encoding_chars,
    get_default_validation_level,
    load_library,
)
from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.exceptions import (
    ManifestMismatch,
    ManifestPrivacyError,
    MissingSchemaDependency,
    SchemaManifestError,
    UnreadableSchemaProfile,
    UnsupportedVersion,
)

__all__ = [
    "MANIFEST_FORMAT",
    "MANIFEST_FORMAT_VERSION",
    "ManifestReport",
    "build_manifest",
    "build_schema_manifest",
    "check_manifest",
    "copy_artifacts",
    "describe_dependencies",
    "dumps_manifest",
    "find_private_values",
    "get_entry",
    "load_entry",
    "load_entry_profile",
    "load_manifest",
    "load_schema_manifest",
    "main",
    "parse_message_with_manifest",
    "read_manifest",
    "required_dependencies",
    "select_entry",
    "verify_manifest",
    "write_manifest",
]

MANIFEST_FORMAT = "hl7apy-schema-manifest"
MANIFEST_FORMAT_VERSION = 1

_KIND_BY_TAG = {"SEG": "segment", "GRP": "group", "FIE": "field", "CMP": "component"}
_STANDARD_TABLE_BY_KIND = {
    "message": "MESSAGES",
    "group": "GROUPS",
    "segment": "SEGMENTS",
    "field": "FIELDS",
    "component": "DATATYPES",
}
_SEQUENCE_TYPES = ("sequence", "choice")
_FORBIDDEN_KEYS = frozenset(
    {
        "value",
        "values",
        "raw",
        "raw_value",
        "er7",
        "mllp",
        "payload",
        "message",
        "message_text",
        "segment_text",
        "field_value",
        "component_value",
        "sample",
        "samples",
        "patient",
        "patients",
        "ssn",
        "dob",
        "address",
        "phone",
    }
)
_HL7_MESSAGE_RE = re.compile(r"(?:^|[\r\n])\s*[A-Z][A-Z0-9]{2}\|")
_VALIDATION_NAMES = {
    VALIDATION_LEVEL.STRICT: "STRICT",
    VALIDATION_LEVEL.TOLERANT: "TOLERANT",
}
_VALIDATION_VALUES = {
    "STRICT": VALIDATION_LEVEL.STRICT,
    "TOLERANT": VALIDATION_LEVEL.TOLERANT,
    "QUIET": VALIDATION_LEVEL.TOLERANT,
}


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _looks_like_message(value):
    return "\x0b" in value or "\x1c" in value or _HL7_MESSAGE_RE.search(value) is not None


def find_private_values(value, path="$"):
    """
    Return the JSON paths that look like message values or patient data.

    The function is recursive and looks for both forbidden keys (``value``,
    ``raw``, ``er7``, ...) and strings that look like ER7 messages. It is used
    before a manifest is written and after it is read, so a schema-only
    manifest cannot accidentally carry a collected message.
    """
    found = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = "{0}.{1}".format(path, key)
            if str(key).lower() in _FORBIDDEN_KEYS:
                found.append(child_path)
            found.extend(find_private_values(child, child_path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(find_private_values(child, "{0}[{1}]".format(path, index)))
    elif isinstance(value, str) and _looks_like_message(value):
        found.append(path)
    return found


def _assert_schema_only(value):
    private = find_private_values(value)
    if private:
        raise ManifestPrivacyError(private[0])


def _schema_node(name, reference, cardinality, tag):
    node = {
        "name": str(name),
        "kind": _KIND_BY_TAG.get(tag, "element"),
        "cardinality": [int(cardinality[0]), int(cardinality[1])],
    }
    if _is_sequence(reference):
        node["content"] = reference[0]
        children = reference[1] or ()
        node["children"] = [
            _schema_node(child[0], child[1], child[2], child[3] if len(child) > 3 else None) for child in children
        ]
    else:
        node["content"] = "leaf"
    if isinstance(reference, (tuple, list)) and len(reference) > 2:
        node["datatype"] = reference[2]
    if isinstance(reference, (tuple, list)) and len(reference) > 3:
        node["long_name"] = reference[3]
    if isinstance(reference, (tuple, list)) and len(reference) > 4:
        node["table"] = reference[4]
    if isinstance(reference, (tuple, list)) and len(reference) > 5:
        node["max_length"] = reference[5]
    return node


def _schema_root(structure, reference):
    node = _schema_node(structure, reference, (1, 1), None)
    node["kind"] = "message"
    return node


def _profile_roots(profile, name):
    if not isinstance(profile, Mapping):
        raise UnreadableSchemaProfile(name, "profile is not a mapping of message structures")
    if not profile:
        raise UnreadableSchemaProfile(name, "profile contains no message structures")
    roots = {}
    for structure, reference in profile.items():
        if not isinstance(reference, (tuple, list)) or not reference:
            raise UnreadableSchemaProfile(name, "invalid reference for {}".format(structure))
        if reference[0] == "mp":
            raise UnreadableSchemaProfile(name, "legacy message profile for {}".format(structure))
        roots[str(structure)] = reference
    return roots


def _standard_reference(kind, name, version):
    table = _STANDARD_TABLE_BY_KIND.get(kind)
    if table is None:
        return None
    return getattr(load_library(version), table).get(name)


def _is_sequence(reference):
    return isinstance(reference, (tuple, list)) and bool(reference) and reference[0] in _SEQUENCE_TYPES


def _standard_content(reference):
    if _is_sequence(reference):
        return reference[0]
    return "leaf"


def _extension_record(node, chain, status, standard=None):
    record = {
        "kind": node["kind"],
        "name": node["name"],
        "path": "/".join(item["name"] for item in chain),
        "status": status,
        "content": node.get("content"),
        "cardinality": list(node["cardinality"]),
        "dependency_chain": list(chain),
    }
    for key in ("datatype", "long_name", "table", "max_length"):
        if key in node:
            record[key] = node[key]
    if standard is not None:
        record["standard"] = {
            "content": _standard_content(standard),
            "datatype": standard[2] if len(standard) > 2 else None,
        }
    return record


def _collect_extensions(node, version, chain=None):
    if chain is None:
        chain = [{"kind": "message", "name": node["name"]}]
    extensions = []
    for child in node.get("children", []):
        child_chain = chain + [{"kind": child["kind"], "name": child["name"]}]
        standard = _standard_reference(child["kind"], child["name"], version)
        if standard is None:
            extensions.append(_extension_record(child, child_chain, "extension"))
            # The whole subtree belongs to the missing extension; listing every
            # descendant would hide the first actionable dependency.
            continue
        if child["kind"] in ("field", "component"):
            standard_datatype = standard[2] if len(standard) > 2 else None
            if child.get("datatype") != standard_datatype or child.get("content") != _standard_content(standard):
                extensions.append(_extension_record(child, child_chain, "override", standard))
                # The subtree is the profile's definition of the overridden
                # datatype. Recording it again would duplicate the dependency.
                continue
        extensions.extend(_collect_extensions(child, version, child_chain))
    return extensions


def _profile_extensions(structure, reference, version):
    root = _schema_root(structure, reference)
    standard = _standard_reference("message", structure, version)
    if standard is None:
        return root, [_extension_record(root, [{"kind": "message", "name": structure}], "extension")]
    return root, _collect_extensions(root, version)


@functools.lru_cache(maxsize=None)
def _library_digest(version):
    lib = load_library(version)
    payload = {
        "version": version,
        "messages": lib.MESSAGES,
        "groups": lib.GROUPS,
        "segments": lib.SEGMENTS,
        "fields": lib.FIELDS,
        "datatypes": lib.DATATYPES,
        "tables": lib.TABLES,
    }
    return _digest(payload)


def _version_key(version):
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def _score_schema(node, version):
    missing = 0
    mismatched = 0
    for child in node.get("children", []):
        standard = _standard_reference(child["kind"], child["name"], version)
        if standard is None:
            missing += 1
            continue
        if child["kind"] in ("field", "component"):
            standard_datatype = standard[2] if len(standard) > 2 else None
            if child.get("datatype") != standard_datatype:
                mismatched += 1
        child_missing, child_mismatched = _score_schema(child, version)
        missing += child_missing
        mismatched += child_mismatched
    return missing, mismatched


def infer_profile_version(structure, reference, candidates=None):
    """
    Infer the HL7 version that best matches a profile schema.

    The score prioritizes elements missing from the standard library, then
    datatype mismatches. The lowest matching version wins ties so that a 2.5
    profile is not reported as 2.5.1 merely because both are compatible.
    """
    candidates = candidates or sorted(SUPPORTED_LIBRARIES, key=_version_key)
    root = _schema_root(structure, reference)
    best = None
    best_key = None
    for version in candidates:
        if structure not in getattr(load_library(version), "MESSAGES"):
            continue
        missing, mismatched = _score_schema(root, version)
        key = (missing, mismatched, _version_key(version))
        if best_key is None or key < best_key:
            best, best_key = version, key
    return best


def _normalize_validation_level(level):
    if level is None:
        level = get_default_validation_level()
    if isinstance(level, str):
        try:
            level = _VALIDATION_VALUES[level.upper()]
        except KeyError:
            raise ValueError("Unknown validation level: {}".format(level))
    check_validation_level(level)
    return int(level)


def _validation_options(validation, version):
    validation = dict(validation or {})
    level = validation.get("validation_level", validation.get("level"))
    level_value = _normalize_validation_level(level)
    find_groups = validation.get("find_groups", True)
    force_validation = validation.get("force_validation", False)
    encoding_chars = validation.get("encoding_chars")
    if encoding_chars is not None:
        encoding_chars = dict(encoding_chars)
        check_encoding_chars(encoding_chars)
    return {
        "level": _VALIDATION_NAMES[level_value],
        "level_value": level_value,
        "find_groups": bool(find_groups),
        "force_validation": bool(force_validation),
        "encoding_chars": encoding_chars,
        "version_default_encoding_chars": dict(get_default_encoding_chars(version)),
    }


def _pickle_protocol(data):
    try:
        for opcode, argument, _ in pickletools.genops(data):
            if opcode.name == "PROTO":
                return int(argument)
            if opcode.name in ("STOP", "FRAME"):
                break
    except Exception:
        pass
    return 0


def _profile_metadata(path):
    with open(path, "rb") as profile_file:
        data = profile_file.read()
    name = os.path.basename(os.fspath(path))
    return {
        "name": name,
        "sha256": _sha256_bytes(data),
        "size": len(data),
        "format": "hl7apy-message-profile",
        "pickle_protocol": _pickle_protocol(data),
    }


class _ArtifactRegistry:
    """
    Assign stable, relative artifact paths and reuse them for identical bytes.
    """

    def __init__(self, directory="profiles"):
        self.directory = directory
        self._by_digest = {}
        self._used_names = set()

    def add(self, path):
        metadata = _profile_metadata(path)
        digest = metadata["sha256"]
        if digest in self._by_digest:
            return dict(self._by_digest[digest])
        name = metadata["name"]
        if name in self._used_names:
            stem, extension = os.path.splitext(name)
            name = "{}-{}{}".format(stem, digest[:8], extension)
        self._used_names.add(name)
        metadata["path"] = "{}/{}".format(self.directory, name)
        self._by_digest[digest] = metadata
        return dict(metadata)


def _normalize_profiles(profiles):
    if isinstance(profiles, (str, os.PathLike)):
        profiles = [profiles]
    if isinstance(profiles, Mapping):
        items = []
        for item_id, value in profiles.items():
            if isinstance(value, Mapping):
                item = dict(value)
            else:
                item = {"path": value}
            item.setdefault("id", str(item_id))
            items.append(item)
        return items
    items = []
    for value in profiles:
        if isinstance(value, (str, os.PathLike)):
            items.append({"path": os.fspath(value)})
        elif isinstance(value, Mapping):
            item = dict(value)
            if "path" not in item and "profile" in item:
                item["path"] = item["profile"]
            items.append(item)
        else:
            raise TypeError("Unsupported profile specification: {!r}".format(value))
    return items


def _item_validation(item, global_validation):
    validation = dict(global_validation or {})
    nested = item.get("validation")
    if isinstance(nested, Mapping):
        validation.update(nested)
    for key in ("validation_level", "level", "find_groups", "force_validation", "encoding_chars"):
        if key in item:
            validation[key] = item[key]
    return validation


def _python_requirement():
    return ">={0}.{1}".format(*sys.version_info[:2])


def _entry_id(item, structure, roots):
    base = item.get("id") or os.path.splitext(os.path.basename(os.fspath(item["path"])))[0]
    if len(roots) == 1:
        return str(base)
    return "{}:{}".format(base, structure)


def _standard_dependency(version):
    return {
        "kind": "hl7-standard",
        "name": "HL7",
        "version": version,
        "module": load_library(version).__name__,
        "digest": _library_digest(version),
    }


def _extensions_with_context(entry_id, structure, version, extensions):
    context = [{"kind": "entry", "name": entry_id}]
    for extension in extensions:
        chain = extension["dependency_chain"]
        if not chain or chain[0].get("kind") != "message":
            chain = [{"kind": "message", "name": structure, "version": version}] + chain
        extension["dependency_chain"] = context + chain
    return extensions


def _build_entry(item, item_id, structure, reference, version, validation, profile_meta, include_schema):
    root, extensions = _profile_extensions(structure, reference, version)
    extensions = _extensions_with_context(item_id, structure, version, extensions)
    schema_digest = _digest(root)
    profile_dependency = {
        "kind": "message-profile",
        "name": profile_meta["name"],
        "artifact": profile_meta["path"],
        "sha256": profile_meta["sha256"],
        "schema_sha256": schema_digest,
    }
    entry = {
        "id": item_id,
        "artifact": profile_meta["path"],
        "message_structure": structure,
        "hl7_version": version,
        "version_source": item.get("version_source", "pinned"),
        "profile": dict(profile_meta, schema_sha256=schema_digest),
        "validation": validation,
        "dependencies": [
            {
                "kind": "python",
                "name": "python",
                "requires": _python_requirement(),
                "observed": platform.python_version(),
            },
            {"kind": "hl7apy", "name": "hl7apy", "version": hl7apy.__version__},
            _standard_dependency(version),
            profile_dependency,
        ],
        "extensions": extensions,
    }
    if include_schema:
        entry["schema"] = root
    return entry


def build_manifest(profiles, validation=None, name=None, version=None, include_schema=False, candidates=None):
    """
    Build a deterministic schema-only manifest for one or more message profiles.

    ``profiles`` may be a path, a list of paths, or a mapping ``id -> path``.
    A mapping value can also be a dictionary with ``path``, ``version`` and
    validation options. Per-profile options override the global ``validation``
    dictionary.

    Profiles with identical bytes are deduplicated into one artifact. Artifact
    paths are relative to the manifest directory so the bundle can be copied to
    another machine. The returned dictionary contains no message values and no
    absolute paths.
    """
    manifest_name = name or "batch"
    items = _normalize_profiles(profiles)
    if not items:
        raise ValueError("At least one profile is required")
    registry = _ArtifactRegistry()
    planned = []
    for item in items:
        path = os.fspath(item["path"])
        profile = hl7apy.load_message_profile(path)
        roots = _profile_roots(profile, os.path.basename(path))
        profile_meta = registry.add(path)
        item_validation = _item_validation(item, validation)
        item_version = item.get("version", version)
        for structure, reference in roots.items():
            if item_version:
                check_version(item_version)
                resolved_version = item_version
                version_source = "pinned"
            else:
                resolved_version = infer_profile_version(structure, reference, candidates)
                if resolved_version is None:
                    raise UnreadableSchemaProfile(
                        os.path.basename(path),
                        "cannot infer HL7 version for message structure {}".format(structure),
                    )
                version_source = "inferred"
            item_with_source = dict(item, version_source=version_source)
            item_id = _entry_id(item_with_source, structure, roots)
            planned.append(
                {
                    "item": item_with_source,
                    "id": item_id,
                    "structure": structure,
                    "reference": reference,
                    "version": resolved_version,
                    "validation": _validation_options(item_validation, resolved_version),
                    "profile": profile_meta,
                }
            )
    planned.sort(
        key=lambda planned_item: (
            planned_item["id"],
            planned_item["structure"],
            _version_key(planned_item["version"]),
            planned_item["profile"]["sha256"],
        )
    )
    seen = {}
    for planned_item in planned:
        key = (planned_item["id"], planned_item["structure"], planned_item["version"])
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            planned_item["id"] = "{}-{}".format(planned_item["id"], seen[key])
    entries = [
        _build_entry(
            planned_item["item"],
            planned_item["id"],
            planned_item["structure"],
            planned_item["reference"],
            planned_item["version"],
            planned_item["validation"],
            planned_item["profile"],
            include_schema,
        )
        for planned_item in planned
    ]
    artifacts = {}
    for entry in entries:
        profile = entry["profile"]
        artifacts[profile["path"]] = {
            "format": profile["format"],
            "sha256": profile["sha256"],
            "size": profile["size"],
            "pickle_protocol": profile["pickle_protocol"],
            "schema_sha256": profile["schema_sha256"],
        }
    manifest = {
        "format": MANIFEST_FORMAT,
        "format_version": MANIFEST_FORMAT_VERSION,
        "name": manifest_name,
        "generator": {
            "name": "hl7apy",
            "version": hl7apy.__version__,
            "python": platform.python_version(),
        },
        "privacy": {
            "message_values": False,
            "policy": "schema-only",
            "forbidden_keys": sorted(_FORBIDDEN_KEYS),
        },
        "summary": {
            "entries": len(entries),
            "versions": sorted({entry["hl7_version"] for entry in entries}, key=_version_key),
            "extensions": sum(len(entry["extensions"]) for entry in entries),
        },
        "artifacts": dict(sorted(artifacts.items())),
        "entries": entries,
    }
    _assert_schema_only(manifest)
    return manifest


def build_schema_manifest(*args, **kwargs):
    """Alias for :func:`build_manifest`."""
    return build_manifest(*args, **kwargs)


def _validate_manifest(manifest):
    if not isinstance(manifest, Mapping):
        raise ManifestMismatch("Schema manifest must be a JSON object")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise ManifestMismatch("Unsupported schema manifest format: {}".format(manifest.get("format")))
    if manifest.get("format_version") != MANIFEST_FORMAT_VERSION:
        raise ManifestMismatch(
            "Unsupported schema manifest version: {}".format(manifest.get("format_version"))
        )
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ManifestMismatch("Schema manifest contains no entries")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ManifestMismatch("Schema manifest entry is not an object")
        for key in ("id", "message_structure", "hl7_version", "profile", "validation", "dependencies", "extensions"):
            if key not in entry:
                raise ManifestMismatch("Schema manifest entry {} is missing {!r}".format(entry.get("id"), key))
        check_version(entry["hl7_version"])
    _assert_schema_only(manifest)


def dumps_manifest(manifest, indent=2):
    """
    Serialize a manifest deterministically as UTF-8 JSON text.

    Keys are sorted and no timestamp is added, so building the same schema on
    two machines produces the same bytes.
    """
    _validate_manifest(manifest)
    return json.dumps(manifest, indent=indent, sort_keys=True, ensure_ascii=False) + "\n"


def write_manifest(manifest, path):
    """Write a schema manifest to ``path`` and return the path."""
    content = dumps_manifest(manifest)
    with open(path, "w", encoding="utf-8", newline="\n") as manifest_file:
        manifest_file.write(content)
    return path


def copy_artifacts(manifest, sources, destination):
    """
    Copy every profile artifact into ``destination`` using its bundle path.

    ``sources`` maps an artifact path (``profiles/<name>``) or a profile name to
    the local file that must be copied. Copies are verified against the manifest
    sha256 before they are written, so a drifted file cannot enter the bundle.
    """
    _validate_manifest(manifest)
    destination = os.fspath(destination)
    for artifact_path, artifact in sorted(manifest.get("artifacts", {}).items()):
        source = sources.get(artifact_path) or sources.get(os.path.basename(artifact_path))
        if source is None:
            source = sources.get(os.path.splitext(os.path.basename(artifact_path))[0])
        if source is None:
            source = artifact_path
        source = os.fspath(source)
        if not os.path.isfile(source):
            raise MissingSchemaDependency(
                _batch_chain(manifest, [{"kind": "artifact", "name": artifact_path}]),
                reason="profile artifact not found",
            )
        with open(source, "rb") as profile_file:
            data = profile_file.read()
        actual = _sha256_bytes(data)
        if artifact.get("sha256") and artifact["sha256"] != actual:
            raise ManifestMismatch(
                "Profile artifact sha256 mismatch (manifest {}, found {})".format(artifact["sha256"], actual),
                _batch_chain(manifest, [{"kind": "artifact", "name": artifact_path}]),
            )
        target = os.path.join(destination, artifact_path)
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "wb") as profile_file:
            profile_file.write(data)
    return destination


def loads_manifest(value):
    """Read a schema manifest from JSON text."""
    try:
        manifest = json.loads(value)
    except ValueError as exc:
        raise ManifestMismatch("Invalid schema manifest JSON: {}".format(exc))
    _validate_manifest(manifest)
    return manifest


def read_manifest(path):
    """Read a schema manifest from ``path``."""
    with open(path, encoding="utf-8") as manifest_file:
        return loads_manifest(manifest_file.read())


load_manifest = read_manifest
load_schema_manifest = read_manifest


class ManifestReport:
    """
    Result of :func:`verify_manifest`.

    ``entries`` contains one summary per verified entry and ``errors`` contains
    the failures that were collected when ``raise_on_error`` is ``False``.
    """

    def __init__(self, name, entries=None, errors=None):
        self.name = name
        self.entries = entries or []
        self.errors = errors or []

    @property
    def ok(self):
        return not self.errors

    def __bool__(self):
        return self.ok

    def __len__(self):
        return len(self.entries)

    def to_dict(self):
        return {"name": self.name, "ok": self.ok, "entries": self.entries, "errors": self.errors}


def _batch_chain(manifest, suffix=None):
    chain = [{"kind": "batch", "name": manifest.get("name", "batch")}]
    if suffix:
        chain.extend(suffix)
    return chain


def _entry_chain(manifest, entry):
    return _batch_chain(
        manifest,
        [
            {"kind": "entry", "name": entry.get("id")},
            {"kind": "profile", "name": entry["profile"].get("name")},
            {"kind": "message", "name": entry["message_structure"], "version": entry["hl7_version"]},
        ],
    )


def _profile_candidates(entry, manifest, base_dir, profiles):
    profile = entry["profile"]
    names = [entry.get("id"), profile.get("name"), entry.get("artifact"), profile.get("path")]
    mapping = {}
    if profiles:
        if isinstance(profiles, Mapping):
            mapping = {str(key): value for key, value in profiles.items()}
        else:
            for item in _normalize_profiles(profiles):
                item_id = item.get("id")
                if item_id:
                    mapping[item_id] = item["path"]
    for name in names:
        if name and name in mapping:
            value = mapping[name]
            if isinstance(value, Mapping):
                value = value.get("path")
            if value:
                yield os.fspath(value)
    if base_dir:
        for name in names:
            if not name:
                continue
            base = os.fspath(base_dir)
            yield os.path.join(base, os.fspath(name))
            basename = os.path.basename(os.fspath(name))
            if basename != name:
                yield os.path.join(base, basename)
            yield os.path.join(base, "profiles", basename)
    for name in names:
        if name:
            yield os.fspath(name)


def _resolve_profile_path(entry, manifest, base_dir, profiles):
    for candidate in _profile_candidates(entry, manifest, base_dir, profiles):
        if os.path.isfile(candidate):
            return candidate
    raise MissingSchemaDependency(
        _entry_chain(manifest, entry),
        reason="profile artifact not found (looked for {})".format(entry.get("artifact")),
    )


def required_dependencies(manifest):
    """
    Return the dependency chain needed to reproduce each entry.

    This works offline: it does not open profile files. It is useful to print
    the complete chain on the machine that produced the collection and to send
    only the missing pieces to the verifying machine.
    """
    _validate_manifest(manifest)
    requirements = []
    seen = set()
    for entry in manifest["entries"]:
        for dependency in entry.get("dependencies", []):
            key = (dependency.get("kind"), dependency.get("name"), dependency.get("version"), dependency.get("artifact"))
            if key in seen:
                continue
            seen.add(key)
            record = {k: v for k, v in dependency.items() if v is not None}
            requirements.append(record)
        for extension in entry.get("extensions", []):
            key = ("extension", extension["path"], extension.get("status"))
            if key in seen:
                continue
            seen.add(key)
            record = {
                "kind": "extension",
                "name": extension["path"],
                "entry": entry["id"],
                "status": extension.get("status"),
                "dependency_chain": extension.get("dependency_chain", []),
            }
            if extension.get("datatype"):
                record["datatype"] = extension["datatype"]
            requirements.append(record)
    return requirements


def describe_dependencies(manifest):
    """Return a human-readable report of :func:`required_dependencies`."""
    lines = []
    for requirement in required_dependencies(manifest):
        if requirement["kind"] == "extension":
            lines.append(
                "extension {name} [{status}] (entry {entry}): {chain}".format(
                    chain=" -> ".join(item["name"] for item in requirement["dependency_chain"]),
                    **requirement
                )
            )
        elif requirement["kind"] == "message-profile":
            lines.append(
                "profile {name} (artifact {artifact}, sha256 {sha256})".format(**requirement)
            )
        elif requirement["kind"] == "hl7-standard":
            lines.append(
                "HL7 {version} standard ({module}, digest {digest})".format(**requirement)
            )
        else:
            lines.append(
                "{kind} {name}: {value}".format(
                    kind=requirement.get("kind"),
                    name=requirement.get("name"),
                    value=requirement.get("version") or requirement.get("requires"),
                )
            )
    return "\n".join(lines)


def _check_entry_versions(manifest, entry, check_versions):
    if not check_versions:
        return
    for dependency in entry.get("dependencies", []):
        if dependency.get("kind") == "hl7apy":
            declared = dependency.get("version")
            installed = hl7apy.__version__
            if declared not in (None, "unknown") and installed not in (None, "unknown") and declared != installed:
                raise ManifestMismatch(
                    "HL7apy version mismatch (manifest {0}, installed {1})".format(declared, installed),
                    _entry_chain(manifest, entry),
                )
        elif dependency.get("kind") == "hl7-standard":
            expected = dependency.get("digest")
            actual = _library_digest(entry["hl7_version"])
            if expected and expected != actual:
                raise ManifestMismatch(
                    "HL7 {} standard schema digest mismatch".format(entry["hl7_version"]),
                    _batch_chain(manifest, [{"kind": "hl7-standard", "name": "HL7", "version": entry["hl7_version"]}]),
                )
        elif dependency.get("kind") == "python":
            requires = dependency.get("requires", ">=3.9")
            match = re.match(r"^>=(\d+)\.(\d+)$", requires)
            if match and sys.version_info[:2] < (int(match.group(1)), int(match.group(2))):
                raise ManifestMismatch(
                    "Python {} is required, running {}".format(requires, platform.python_version()),
                    _entry_chain(manifest, entry),
                )


def _current_extensions(structure, reference, version):
    return _profile_extensions(structure, reference, version)[1]


def _verify_entry(manifest, entry, base_dir, profiles, check_versions, require_exact_profile):
    _check_entry_versions(manifest, entry, check_versions)
    profile_path = _resolve_profile_path(entry, manifest, base_dir, profiles)
    with open(profile_path, "rb") as profile_file:
        data = profile_file.read()
    expected_sha = entry["profile"].get("sha256")
    actual_sha = _sha256_bytes(data)
    if require_exact_profile and expected_sha and expected_sha != actual_sha:
        raise ManifestMismatch(
            "Profile artifact sha256 mismatch (manifest {}, found {})".format(expected_sha, actual_sha),
            _entry_chain(manifest, entry),
        )
    try:
        profile = hl7apy.load_message_profile(profile_path)
    except Exception as exc:
        raise MissingSchemaDependency(
            _entry_chain(manifest, entry), reason="unreadable profile: {}".format(exc)
        )
    roots = _profile_roots(profile, os.path.basename(profile_path))
    reference = roots.get(entry["message_structure"])
    if reference is None:
        raise MissingSchemaDependency(
            _entry_chain(manifest, entry),
            reason="message structure {} is not defined in the profile".format(entry["message_structure"]),
        )
    current = _current_extensions(entry["message_structure"], reference, entry["hl7_version"])
    current = _extensions_with_context(
        entry["id"], entry["message_structure"], entry["hl7_version"], current
    )
    current_by_path = {extension["path"]: extension for extension in current}
    expected_by_path = {extension["path"]: extension for extension in entry.get("extensions", [])}
    for path, expected in expected_by_path.items():
        if path not in current_by_path:
            chain = expected.get("dependency_chain") or _entry_chain(manifest, entry)
            raise MissingSchemaDependency(
                chain,
                reason="extension {} is not defined by profile {}".format(path, entry["profile"].get("name")),
            )
    for path, extension in current_by_path.items():
        if path not in expected_by_path:
            chain = extension.get("dependency_chain") or _entry_chain(manifest, entry)
            raise ManifestMismatch("Profile contains undeclared extension {}".format(path), chain)
    root = _schema_root(entry["message_structure"], reference)
    actual_schema_digest = _digest(root)
    expected_schema_digest = entry["profile"].get("schema_sha256")
    if expected_schema_digest and actual_schema_digest != expected_schema_digest:
        raise ManifestMismatch(
            "Profile schema digest mismatch (manifest {}, found {})".format(
                expected_schema_digest, actual_schema_digest
            ),
            _entry_chain(manifest, entry),
        )
    return {
        "id": entry["id"],
        "message_structure": entry["message_structure"],
        "hl7_version": entry["hl7_version"],
        "profile": entry["profile"].get("path"),
        "profile_sha256": actual_sha,
        "schema_sha256": actual_schema_digest,
        "extensions": len(current),
        "validation": entry["validation"],
    }


def verify_manifest(
    manifest,
    base_dir=None,
    profiles=None,
    check_versions=True,
    require_exact_profile=True,
    raise_on_error=True,
):
    """
    Verify that every manifest entry can be reproduced locally.

    A missing profile, message structure or extension raises
    :class:`~hl7apy.exceptions.MissingSchemaDependency` with the complete
    dependency chain. The standard HL7 schema is never used as a fallback.

    When ``raise_on_error`` is ``False`` all entries are checked and failures
    are collected in the returned :class:`ManifestReport`.
    """
    if isinstance(manifest, (str, os.PathLike)):
        manifest_path = os.fspath(manifest)
        if base_dir is None:
            base_dir = os.path.dirname(os.path.abspath(manifest_path))
        manifest = read_manifest(manifest_path)
    else:
        _validate_manifest(manifest)
    entries = []
    errors = []
    for entry in manifest["entries"]:
        try:
            entries.append(
                _verify_entry(manifest, entry, base_dir, profiles, check_versions, require_exact_profile)
            )
        except SchemaManifestError as exc:
            error = {
                "id": entry.get("id"),
                "message_structure": entry.get("message_structure"),
                "hl7_version": entry.get("hl7_version"),
                "type": type(exc).__name__,
                "error": str(exc),
            }
            if isinstance(exc, MissingSchemaDependency):
                error["chain"] = exc.chain
            errors.append(error)
            if raise_on_error:
                raise
    return ManifestReport(manifest.get("name", "batch"), entries, errors)


def check_manifest(*args, **kwargs):
    """Alias for :func:`verify_manifest`."""
    kwargs.setdefault("raise_on_error", False)
    return verify_manifest(*args, **kwargs)


def select_entry(manifest, message_structure=None, hl7_version=None, entry_id=None):
    """
    Select exactly one entry by id or by message structure and HL7 version.

    No entry is selected by version range or by falling back to another
    standard version. A missing selection raises
    :class:`~hl7apy.exceptions.MissingSchemaDependency`.
    """
    if isinstance(manifest, (str, os.PathLike)):
        manifest = read_manifest(manifest)
    else:
        _validate_manifest(manifest)
    matches = []
    for entry in manifest["entries"]:
        if entry_id is not None and entry["id"] != entry_id:
            continue
        if message_structure is not None and entry["message_structure"].upper() != message_structure.upper():
            continue
        if hl7_version is not None and entry["hl7_version"] != hl7_version:
            continue
        matches.append(entry)
    if not matches:
        chain = _batch_chain(manifest)
        if entry_id is not None:
            chain.append({"kind": "entry", "name": entry_id})
        if message_structure is not None:
            chain.append({"kind": "message", "name": message_structure, "version": hl7_version})
        raise MissingSchemaDependency(chain, reason="no matching manifest entry")
    if len(matches) > 1:
        raise ManifestMismatch(
            "Ambiguous manifest entries for message {} in version {}".format(message_structure, hl7_version),
            _batch_chain(manifest),
        )
    return matches[0]


get_entry = select_entry


def load_entry(manifest, entry_id=None, message_structure=None, hl7_version=None):
    """Return the manifest entry selected by :func:`select_entry`."""
    return select_entry(manifest, message_structure, hl7_version, entry_id)


def _resolve_entry_profile(entry, manifest, base_dir, profiles, require_exact_profile):
    if isinstance(manifest, (str, os.PathLike)):
        manifest_path = os.fspath(manifest)
        if base_dir is None:
            base_dir = os.path.dirname(os.path.abspath(manifest_path))
        manifest = read_manifest(manifest_path)
    _verify_entry(manifest, entry, base_dir, profiles, True, require_exact_profile)
    profile_path = _resolve_profile_path(entry, manifest, base_dir, profiles)
    return hl7apy.load_message_profile(profile_path)


def load_entry_profile(
    manifest,
    entry_id=None,
    message_structure=None,
    hl7_version=None,
    base_dir=None,
    profiles=None,
    require_exact_profile=True,
):
    """Load and verify the profile for one manifest entry."""
    entry = select_entry(manifest, message_structure, hl7_version, entry_id)
    return _resolve_entry_profile(entry, manifest, base_dir, profiles, require_exact_profile)


def parse_message_with_manifest(message, manifest, base_dir=None, profiles=None, require_exact_profile=True):
    """
    Parse a message using the profile and validation options from a manifest.

    The entry is selected by the message structure and the exact HL7 version
    read from MSH-9/MSH-12. If the entry, profile or an extension is missing,
    the parse fails with a dependency chain instead of using the standard HL7
    schema.
    """
    from hl7apy.parser import get_message_info, parse_message

    if isinstance(manifest, (str, os.PathLike)):
        manifest_path = os.fspath(manifest)
        if base_dir is None:
            base_dir = os.path.dirname(os.path.abspath(manifest_path))
        manifest = read_manifest(manifest_path)
    else:
        _validate_manifest(manifest)
    _, message_structure, version = get_message_info(message)
    entry = select_entry(manifest, message_structure, version)
    profile = _resolve_entry_profile(entry, manifest, base_dir, profiles, require_exact_profile)
    options = entry["validation"]
    return parse_message(
        message,
        validation_level=options["level_value"],
        find_groups=options["find_groups"],
        message_profile=profile,
        force_validation=options["force_validation"],
    )


def _parse_profile_argument(value):
    if "=" in value and not os.path.exists(value):
        item_id, path = value.split("=", 1)
        return {"id": item_id, "path": path}
    return {"path": value}


def _artifact_sources(manifest, profiles):
    sources = {}
    items = _normalize_profiles(profiles)
    artifacts = manifest.get("artifacts", {})
    for item in items:
        path = os.fspath(item["path"])
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as profile_file:
            digest = _sha256_bytes(profile_file.read())
        for artifact_path, artifact in artifacts.items():
            if artifact.get("sha256") == digest:
                sources[artifact_path] = path
    return sources


def _build_command(args):
    raw_profiles = list(args.profiles) + list(args.profile)
    if not raw_profiles:
        raise ValueError("At least one profile is required")
    profiles = [_parse_profile_argument(value) for value in raw_profiles]
    validation = {
        "find_groups": not args.no_find_groups,
        "force_validation": args.force_validation,
    }
    if args.validation_level:
        validation["validation_level"] = args.validation_level
    manifest = build_manifest(
        profiles,
        validation=validation,
        name=args.name,
        version=args.version,
        include_schema=args.include_schema,
    )
    content = dumps_manifest(manifest)
    if args.output == "-":
        sys.stdout.write(content)
    else:
        write_manifest(manifest, args.output)
        sys.stderr.write("Wrote {}\n".format(args.output))
    if args.bundle:
        if args.output == "-":
            raise ValueError("--bundle requires -o/--output")
        bundle_dir = os.path.dirname(os.path.abspath(args.output))
        copy_artifacts(manifest, _artifact_sources(manifest, profiles), bundle_dir)
        sys.stderr.write("Copied profile artifacts to {}\n".format(os.path.join(bundle_dir, "profiles")))
    return 0


def _verify_command(args):
    profiles = [_parse_profile_argument(value) for value in args.profile]
    report = verify_manifest(
        args.manifest,
        base_dir=args.profiles_dir,
        profiles=profiles or None,
        check_versions=not args.no_check_versions,
        require_exact_profile=not args.allow_artifact_drift,
        raise_on_error=False,
    )
    if args.json:
        sys.stdout.write(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    else:
        for entry in report.entries:
            sys.stdout.write(
                "OK {id} {message_structure} {hl7_version} extensions={extensions}\n".format(**entry)
            )
        for error in report.errors:
            sys.stderr.write("ERROR {id}: {error}\n".format(**error))
    return 0 if report.ok else 1


def _deps_command(args):
    manifest = read_manifest(args.manifest)
    if args.json:
        sys.stdout.write(json.dumps(required_dependencies(manifest), indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(describe_dependencies(manifest) + "\n")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m hl7apy.manifest", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="build a schema-only manifest")
    build_parser.add_argument("profiles", nargs="*", help="profile path or ID=PATH")
    build_parser.add_argument("-p", "--profile", action="append", default=[], help="profile path or ID=PATH")
    build_parser.add_argument("-o", "--output", default="-", help="output file, or - for stdout")
    build_parser.add_argument("--name", default="batch", help="manifest/batch name")
    build_parser.add_argument("--version", default=None, help="pin the HL7 version (e.g. 2.5)")
    build_parser.add_argument(
        "--validation-level", choices=["strict", "tolerant"], default=None, help="validation level"
    )
    build_parser.add_argument("--no-find-groups", action="store_true", help="disable automatic group discovery")
    build_parser.add_argument("--force-validation", action="store_true", help="validate while parsing")
    build_parser.add_argument("--include-schema", action="store_true", help="embed the normalized schema tree")
    build_parser.add_argument(
        "--bundle", action="store_true", help="copy profile artifacts next to the manifest (requires -o)"
    )
    build_parser.set_defaults(func=_build_command)

    verify_parser = subparsers.add_parser("verify", help="verify a manifest against local artifacts")
    verify_parser.add_argument("manifest", help="manifest file")
    verify_parser.add_argument("--profiles-dir", default=None, help="directory containing profile artifacts")
    verify_parser.add_argument("-p", "--profile", action="append", default=[], help="profile override ID=PATH")
    verify_parser.add_argument("--no-check-versions", action="store_true", help="skip version pin checks")
    verify_parser.add_argument(
        "--allow-artifact-drift", action="store_true", help="accept different profile bytes if the schema matches"
    )
    verify_parser.add_argument("--json", action="store_true", help="print a JSON report")
    verify_parser.set_defaults(func=_verify_command)

    deps_parser = subparsers.add_parser("deps", help="print the schema dependency chain (offline)")
    deps_parser.add_argument("manifest", help="manifest file")
    deps_parser.add_argument("--json", action="store_true", help="print machine-readable dependencies")
    deps_parser.set_defaults(func=_deps_command)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SchemaManifestError as exc:
        sys.stderr.write("error: {}\n".format(exc))
        return 2
    except (ValueError, OSError, UnsupportedVersion) as exc:
        sys.stderr.write("error: {}\n".format(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
