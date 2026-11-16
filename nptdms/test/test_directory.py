"""Tests for the structured channel directory and round-trippable paths"""

import pytest

from nptdms import tdms
from nptdms.common import ObjectPath
from nptdms.directory import ChannelDirectory
from nptdms.test.util import (
    GeneratedFile, channel_metadata, hexlify_value, segment_objects_metadata)


TRICKY_NAMES = [
    "group/one",
    "group 'quoted'",
    "group with spaces",
    "group/'mix ed'",
    "a/b/c",
    "''",
]


def _segment_with_channels(*paths):
    toc = ("kTocMetaData", "kTocRawData", "kTocNewObjList")
    metadata = segment_objects_metadata(*[
        channel_metadata(str(path), 3, 2) for path in paths])
    data = "".join(hexlify_value('<i', v) for _ in paths for v in (1, 2))
    test_file = GeneratedFile()
    test_file.add_segment(toc, metadata, data)
    return test_file


def _load_directory(*paths):
    test_file = _segment_with_channels(*paths)
    tdms_file = test_file.load(read_metadata_only=True)
    return ChannelDirectory(tdms_file)


@pytest.mark.parametrize("name", TRICKY_NAMES)
def test_object_path_round_trip(name):
    """Paths with slashes, quotes and spaces survive a string round trip"""
    group_path = ObjectPath(name)
    assert ObjectPath.from_string(str(group_path)) == group_path
    channel_path = ObjectPath(name, name)
    parsed = ObjectPath.from_string(str(channel_path))
    assert parsed == channel_path
    assert parsed.components == (name, name)


def test_object_path_equality_and_hash():
    assert ObjectPath("a", "b") == ObjectPath("a", "b")
    assert ObjectPath("a", "b") != ObjectPath("a/b")
    assert hash(ObjectPath("a", "b")) == hash(ObjectPath("a", "b"))
    assert ObjectPath("a", "b") != "not a path"


def test_object_path_renamed_only_changes_leaf_level():
    assert ObjectPath("g", "c").renamed("new") == ObjectPath("g", "new")
    assert ObjectPath("g").renamed("new") == ObjectPath("new")
    with pytest.raises(ValueError):
        ObjectPath().renamed("new")


def test_directory_built_from_tdms_file():
    group_name = "group/one 'q'"
    channel_name = "chan nel/'x'"
    directory = _load_directory(ObjectPath(group_name, channel_name))

    assert len(directory) == 2
    assert len(directory.groups()) == 1
    assert len(directory.channels()) == 1
    assert directory.child_names() == [group_name]
    assert directory.child_names(ObjectPath(group_name)) == [channel_name]


def test_lookup_by_path_object_string_and_components():
    group_name = "group/one"
    channel_name = "chan 'nel'"
    path = ObjectPath(group_name, channel_name)
    directory = _load_directory(path)

    by_object = directory[path]
    assert by_object is directory[str(path)]
    assert by_object is directory[(group_name, channel_name)]
    assert by_object is directory[[group_name, channel_name]]
    assert path in directory
    assert str(path) in directory
    assert ObjectPath("missing") not in directory
    assert directory.get(ObjectPath("missing")) is None
    with pytest.raises(KeyError):
        directory[ObjectPath("missing")]


def test_original_path_literal_lookup():
    """The original TDMS path literal from an object can be used to look it up"""
    directory = _load_directory(ObjectPath("g/r '1'", "c h"))
    group = directory.groups()[0]
    channel = directory.channels()[0]

    assert directory[group.path] is group
    assert directory[channel.path] is channel


def test_similar_display_paths_are_not_merged():
    """Objects with similar display paths but different hierarchy stay distinct"""
    nested_channel = ObjectPath("a", "b/c")
    deep_channel = ObjectPath("a/b", "c")
    directory = _load_directory(nested_channel, deep_channel)

    assert len(directory) == 4
    assert len(directory.channels()) == 2
    assert directory[nested_channel] is not directory[deep_channel]
    assert directory.child_names(ObjectPath("a")) == ["b/c"]
    assert directory.child_names(ObjectPath("a/b")) == ["c"]
    # Both would display as "a/b/c" if naively joined, but paths stay distinct
    assert nested_channel != deep_channel
    assert str(nested_channel) != str(deep_channel)


def test_rename_group_only_changes_group_level():
    test_file = _segment_with_channels(
        ObjectPath("group", "channel one"),
        ObjectPath("group", "channel two"),
        ObjectPath("group/x", "channel one"),
    )
    tdms_file = test_file.load(read_metadata_only=True)
    directory = ChannelDirectory(tdms_file)

    directory.rename(ObjectPath("group"), "new group")

    assert ObjectPath("new group") in directory
    assert ObjectPath("group") not in directory
    # Channels keep their own names and move with the group
    assert directory.child_names(ObjectPath("new group")) == ["channel one", "channel two"]
    assert ObjectPath("new group", "channel one") in directory
    # The similarly named group is untouched
    assert ObjectPath("group/x", "channel one") in directory
    assert sorted(directory.child_names()) == ["group/x", "new group"]


def test_rename_channel_only_changes_channel_level():
    directory = _load_directory(
        ObjectPath("group", "channel"),
        ObjectPath("group", "channel/2"))

    directory.rename("/'group'/'channel'", "renamed")

    assert ObjectPath("group", "renamed") in directory
    assert ObjectPath("group", "channel") not in directory
    # Sibling channel and group level are unchanged
    assert ObjectPath("group", "channel/2") in directory
    assert directory.child_names() == ["group"]


def test_rename_to_slash_containing_name_does_not_change_hierarchy():
    directory = _load_directory(ObjectPath("g", "c"))

    directory.rename(ObjectPath("g"), "a/b")

    assert ObjectPath("a/b", "c") in directory
    assert ObjectPath("a/b") in directory
    # The renamed group is still a single level, not two nested groups
    assert directory.child_names() == ["a/b"]
    assert ObjectPath("a") not in directory


def test_rename_collision_raises_and_does_not_merge():
    directory = _load_directory(
        ObjectPath("group", "channel"),
        ObjectPath("group", "other"))

    with pytest.raises(ValueError):
        directory.rename(ObjectPath("group", "channel"), "other")

    # Both original objects are still present and distinct
    assert len(directory) == 3
    assert ObjectPath("group", "channel") in directory
    assert ObjectPath("group", "other") in directory


def test_rename_missing_object_raises():
    directory = _load_directory(ObjectPath("group", "channel"))

    with pytest.raises(KeyError):
        directory.rename(ObjectPath("group", "missing"), "new")
    with pytest.raises(KeyError):
        directory.rename(ObjectPath("missing group"), "new")
    with pytest.raises(ValueError):
        directory.rename(ObjectPath(), "new")


def test_add_duplicate_path_raises():
    directory = ChannelDirectory()
    directory.add(ObjectPath("g", "c"), object())
    with pytest.raises(ValueError):
        directory.add(ObjectPath("g", "c"), object())


def test_directory_paths_round_trip():
    paths = [ObjectPath(name, name) for name in TRICKY_NAMES]
    directory = _load_directory(*paths)

    for path in directory.paths():
        assert ObjectPath.from_string(str(path)) == path
    assert set(directory.paths()) == set(paths) | set(
        ObjectPath(name) for name in TRICKY_NAMES)


def test_directory_from_mapping():
    obj = object()
    directory = ChannelDirectory({ObjectPath("g", "c"): obj})
    assert directory[ObjectPath("g", "c")] is obj
    directory = ChannelDirectory([(ObjectPath("g", "c"), obj)])
    assert directory["/'g'/'c'"] is obj
