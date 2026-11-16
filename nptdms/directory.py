""" Structured directory of TDMS objects

    Provides a tree view of the groups and channels in a TDMS file where
    objects are keyed by their exact path components. Group and channel names
    containing slashes, quotes or spaces are handled unambiguously, so two
    objects whose paths merely look similar when displayed are never merged,
    and objects can always be looked up by their original TDMS path string.
"""

from nptdms.common import ObjectPath


_MISSING = object()


class ChannelDirectory(object):
    """ A structured directory of the groups and channels in a TDMS file

        :param objects: Optional source of objects to populate the directory
            with. Either a :class:`~nptdms.TdmsFile`, a mapping of object
            paths to objects, or an iterable of ``(path, object)`` pairs.
            Paths may be :class:`~nptdms.common.ObjectPath` instances, TDMS
            path strings such as ``/'group'/'channel'``, or sequences of
            name components.
    """

    def __init__(self, objects=None):
        self._root = _DirectoryNode()
        if objects is None:
            return
        if hasattr(objects, "groups"):
            for group in objects.groups():
                self.add(group.path, group)
                for channel in group.channels():
                    self.add(channel.path, channel)
        elif hasattr(objects, "items"):
            for path, obj in objects.items():
                self.add(path, obj)
        else:
            for path, obj in objects:
                self.add(path, obj)

    def add(self, path, obj):
        """ Add an object to the directory

            :param path: The object's path, as an ObjectPath, a TDMS path
                string or a sequence of name components.
            :param obj: The object to store at that path.
            :raises ValueError: If an object already exists at the path.
        """
        path = _as_object_path(path)
        node = self._node_for(path.components, create=True)
        if node.value is not _MISSING:
            raise ValueError("An object already exists at path {0}".format(path))
        node.value = obj

    def rename(self, path, new_name):
        """ Rename an object, changing only the level identified by the path

            Renaming a group moves its channels with it but leaves their
            names unchanged, and renaming a channel leaves its group
            unchanged. Other objects are never affected, even if their
            displayed paths look similar.

            :param path: Path of the object to rename, as an ObjectPath, a
                TDMS path string or a sequence of name components.
            :param new_name: The new name for that level of the path.
            :raises KeyError: If there is no object at the path.
            :raises ValueError: If the path is the root object, or an object
                already exists with the new name at that level.
        """
        path = _as_object_path(path)
        if path.is_root:
            raise ValueError("Cannot rename the root object")
        parent = self._node_for(path.parent.components)
        if parent is None:
            raise KeyError("No object found at path {0}".format(path))
        old_name = path.components[-1]
        node = parent.children.get(old_name)
        if node is None or node.value is _MISSING:
            raise KeyError("No object found at path {0}".format(path))
        if new_name != old_name and new_name in parent.children:
            raise ValueError(
                "An object already exists at path {0}".format(path.renamed(new_name)))
        if new_name != old_name:
            parent.children[new_name] = parent.children.pop(old_name)

    def get(self, path, default=None):
        """ Get an object from the directory, returning a default if not found

            :param path: The object's path, as an ObjectPath, a TDMS path
                string or a sequence of name components.
        """
        try:
            return self[path]
        except KeyError:
            return default

    def child_names(self, path=()):
        """ The names of the direct children of the object at a path

            :param path: The parent path, as an ObjectPath, a TDMS path
                string or a sequence of name components. Defaults to the
                root, listing the top level group names.
            :raises KeyError: If there is no object at the path.
        """
        path = _as_object_path(path)
        node = self._node_for(path.components)
        if node is None or (path.components and node.value is _MISSING):
            raise KeyError("No object found at path {0}".format(path))
        return list(node.children)

    def groups(self):
        """ The objects at the group level of the directory
        """
        return [obj for path, obj in self.items() if path.is_group]

    def channels(self):
        """ The objects at the channel level of the directory
        """
        return [obj for path, obj in self.items() if path.is_channel]

    def paths(self):
        """ The paths of all objects in the directory as ObjectPaths

            Each returned path round-trips: converting it to a string and
            parsing it again yields an equal path.
        """
        return [path for path, _ in self.items()]

    def items(self):
        """ ``(ObjectPath, object)`` pairs for all objects in the directory
        """
        result = []
        self._collect(self._root, (), result)
        return result

    def _collect(self, node, components, result):
        if node.value is not _MISSING:
            result.append((ObjectPath(*components), node.value))
        for name, child in node.children.items():
            self._collect(child, components + (name,), result)

    def _node_for(self, components, create=False):
        node = self._root
        for component in components:
            if create:
                node = node.children.setdefault(component, _DirectoryNode())
            else:
                node = node.children.get(component)
                if node is None:
                    return None
        return node

    def __getitem__(self, path):
        path = _as_object_path(path)
        node = self._node_for(path.components)
        if node is None or node.value is _MISSING:
            raise KeyError("No object found at path {0}".format(path))
        return node.value

    def __contains__(self, path):
        try:
            path = _as_object_path(path)
        except ValueError:
            return False
        node = self._node_for(path.components)
        return node is not None and node.value is not _MISSING

    def __iter__(self):
        return iter(self.paths())

    def __len__(self):
        return sum(1 for _ in self.items())


class _DirectoryNode(object):
    __slots__ = ("children", "value")

    def __init__(self):
        self.children = {}
        self.value = _MISSING


def _as_object_path(path):
    if isinstance(path, ObjectPath):
        return path
    if isinstance(path, str):
        return ObjectPath.from_string(path)
    return ObjectPath(*path)
