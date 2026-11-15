import datetime
import logging
import random
import string

logger = logging.getLogger(__file__)


def ishl7(line):
    """Determines whether a *line* looks like an HL7 message.
    This method only does a cursory check and does not fully
    validate the message.

    :rtype: bool
    """
    # Prevent issues if the line is empty
    if not line:
        return False
    msh = line.strip()[:4]
    if len(msh) != 4:
        return False
    return msh[:3] == "MSH" and line.count("\rMSH" + msh[3]) == 0


def isbatch(line):
    """
    Batches are wrapped in BHS / BTS or have more than one
    message
    BHS = batch header segment
    BTS = batch trailer segment
    """
    return line and (
        line.strip()[:3] == "BHS"
        or (line.count("MSH") > 1 and line.strip()[:3] != "FHS")
    )


def isfile(line):
    """
    Files are wrapped in FHS / FTS, or may be a batch
    FHS = file header segment
    FTS = file trailer segment
    """
    return line and (line.strip()[:3] == "FHS" or isbatch(line))


def split_file(hl7file):
    """
    Given a file, split out the messages.
    Does not do any validation on the message.
    Throws away batch and file segments.
    """
    rv = []
    for line in hl7file.split("\r"):
        line = line.strip()
        if line[:3] in ["FHS", "BHS", "FTS", "BTS"]:
            continue
        if line[:3] == "MSH":
            newmsg = [line]
            rv.append(newmsg)
        else:
            if len(rv) == 0:
                logger.error("Segment received before message header [%s]", line)
                continue
            rv[-1].append(line)
    rv = ["\r".join(msg) for msg in rv]
    for i, msg in enumerate(rv):
        if not msg[-1] == "\r":
            rv[i] = msg + "\r"
    return rv


alphanumerics = string.ascii_uppercase + string.digits


def generate_message_control_id():
    """Generate a unique 20 character message id.

    See http://www.hl7resources.com/Public/index.html?a55433.htm
    """
    d = datetime.datetime.utcnow()
    # Strip off the decade, ID only has to be unique for 3 years.
    # So now we have a 16 char timestamp.
    timestamp = d.strftime("%y%j%H%M%S%f")[1:]
    # Add 4 chars of uniqueness
    unique = "".join(random.sample(alphanumerics, 4))
    return timestamp + unique


def escape(container, field, app_map=None):
    """
    See: http://www.hl7standards.com/blog/2006/11/02/hl7-escape-sequences/

    To process this correctly, the full set of separators (MSH.1/MSH.2) needs to be known.

    Pass through the message. Replace recognised characters with their escaped
    version. Return an ascii encoded string.

    Functionality:

    *   Replace separator characters (2.10.4)
    *   replace application defined characters (2.10.7)
    *   Replace non-ascii values with hex versions using HL7 conventions.

    Incomplete:

    *   replace highlight characters (2.10.3)
    *   How to handle the rich text substitutions.
    *   Merge contiguous hex values
    """
    if not field:
        return field

    esc = str(container.esc)

    DEFAULT_MAP = {
        container.separators[1]: "F",  # 2.10.4
        container.separators[2]: "R",
        container.separators[3]: "S",
        container.separators[4]: "T",
        container.esc: "E",
        "\r": ".br",  # 2.10.6
    }

    rv = []
    for offset, c in enumerate(field):
        if app_map and c in app_map:
            rv.append(esc + app_map[c] + esc)
        elif c in DEFAULT_MAP:
            rv.append(esc + DEFAULT_MAP[c] + esc)
        elif ord(c) >= 0x20 and ord(c) <= 0x7E:
            rv.append(c)
        elif ord(c) > 0xFF:
            # Multi-byte characters are hex encoded as UTF-8 bytes
            rv.append("%sX%s%s" % (esc, c.encode("utf-8").hex(), esc))
        else:
            rv.append("%sX%2x%s" % (esc, ord(c), esc))

    return "".join(rv)


class Fragment:
    """One piece of a formatted HL7 text value (HL7 Standard, Section 2.10),
    as produced by :func:`unescape_fragments`.

    A formatted (e.g. ``FT``) value is decoded into a list of fragments:

    *   ``Fragment.TEXT`` -- business text.  Escape sequences such as
        ``\\F\\`` are decoded into the separator character, which remains
        part of the value and is never treated as a delimiter.
    *   ``Fragment.FORMAT`` -- a formatting instruction (2.10.3, 2.10.6),
        e.g. ``H`` (highlight on), ``N`` (highlight off), ``.br`` (line break).
    *   ``Fragment.LOCAL`` -- a locally defined (``\\Z...\\``) or otherwise
        unrecognized escape sequence, preserved verbatim as evidence.
    """

    TEXT = "text"
    FORMAT = "format"
    LOCAL = "local"

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value

    def __eq__(self, other):
        if isinstance(other, Fragment):
            return self.kind == other.kind and self.value == other.value
        return NotImplemented

    def __hash__(self):
        return hash((self.kind, self.value))

    def __repr__(self):
        return "Fragment({0!r}, {1!r})".format(self.kind, self.value)


#: Formatting instructions recognized by :func:`unescape_fragments`
#: (HL7 Standard, Sections 2.10.3 and 2.10.6)
FORMATTING_SEQUENCES = (
    "H",
    "N",
    ".br",
    ".sp",
    ".fi",
    ".nf",
    ".in",
    ".ti",
    ".sk",
    ".ce",
)


def unescape_fragments(container, field, app_map=None):  # noqa: C901
    """Decode a formatted HL7 text value (e.g. an ``FT`` field) into a list
    of :py:class:`hl7.Fragment` text and formatting pieces.

    To process this correctly, the full set of separators (MSH.1/MSH.2) needs
    to be known, which is taken from ``container``.

    *   Separator escapes (2.10.4, e.g. ``\\F\\``) are decoded into the
        separator character as part of a ``TEXT`` fragment -- the decoded
        separator still belongs to the value and is never split into columns.
    *   Highlighting (2.10.3) and rich text (2.10.6) instructions become
        ``FORMAT`` fragments.
    *   Hex encoded bytes (2.10.5) are decoded into ``TEXT`` fragments.
    *   Application defined mappings (2.10.7) provided via ``app_map`` are
        decoded into ``TEXT`` fragments.
    *   Unknown local escapes (``\\Z...\\``) and any other unrecognized
        sequence are preserved verbatim as ``LOCAL`` fragments.

    Use :func:`escape_fragments` to encode the fragments back into an HL7
    string; the round trip preserves the business content of every fragment.

    :rtype: list of :py:class:`hl7.Fragment`
    """
    if not field:
        return []

    esc = container.esc
    if field.find(esc) == -1:
        return [Fragment(Fragment.TEXT, field)]

    SEPARATOR_MAP = {
        "F": container.separators[1],  # 2.10.4
        "R": container.separators[2],
        "S": container.separators[3],
        "T": container.separators[4],
        "E": esc,
    }

    fragments = []
    text = []
    collecting = []
    in_seq = False

    def flush_text():
        if text:
            fragments.append(Fragment(Fragment.TEXT, "".join(text)))
            del text[:]

    for offset, c in enumerate(field):
        if in_seq:
            if c == esc:
                in_seq = False
                value = "".join(collecting)
                collecting = []
                if not value:
                    logger.warning(
                        "Error unescaping value [%s], empty sequence found at %d",
                        field,
                        offset,
                    )
                    continue
                if app_map and value in app_map:
                    # Application defined mapping (2.10.7)
                    text.append(app_map[value])
                elif value in SEPARATOR_MAP:
                    # The decoded separator remains part of the value (2.10.4)
                    text.append(SEPARATOR_MAP[value])
                elif value in FORMATTING_SEQUENCES or (
                    value.startswith(".") and value[:3] in FORMATTING_SEQUENCES
                ):
                    # Highlighting (2.10.3) and rich text (2.10.6), keeping
                    # any repetition count (e.g. ".in4")
                    flush_text()
                    fragments.append(Fragment(Fragment.FORMAT, value))
                elif value[0] == "C" or value[0] == "M":
                    # Inline character set switching (2.10.2) not implemented
                    logger.warning(
                        "Error inline character sets [%s] not implemented, field [%s], offset [%s]",
                        value,
                        field,
                        offset,
                    )
                elif value[0] == "X":  # Hex encoded Bytes: 2.10.5
                    hexpart = value[1:]
                    try:
                        data = bytes(
                            int(hexpart[off : off + 2], 16)
                            for off in range(0, len(hexpart), 2)
                        )
                    except Exception:
                        logger.exception(
                            "Error decoding hex value [%s], field [%s], offset [%s]",
                            hexpart,
                            field,
                            offset,
                        )
                    else:
                        try:
                            text.append(data.decode("utf-8"))
                        except UnicodeDecodeError:
                            # Single byte escapes (e.g. \Xe1\) are latin-1
                            text.append(data.decode("latin-1"))
                else:
                    # Unknown local escapes (\Z...\) and other unrecognized
                    # sequences are preserved verbatim as evidence
                    flush_text()
                    fragments.append(Fragment(Fragment.LOCAL, value))
            else:
                collecting.append(c)
        elif c == esc:
            in_seq = True
        else:
            text.append(str(c))
    flush_text()
    return fragments


def escape_fragments(container, fragments, app_map=None):
    """Encode a list of :py:class:`hl7.Fragment` back into an HL7 string,
    the inverse of :func:`unescape_fragments`.

    *   ``TEXT`` fragments are escaped with :func:`escape`, so a decoded
        separator (e.g. ``|`` from ``\\F\\``) is re-escaped and cannot be
        split into columns when the message is parsed again.
    *   ``FORMAT`` fragments are emitted as their escape sequence
        (e.g. ``\\H\\``, ``\\.br\\``).
    *   ``LOCAL`` fragments are emitted verbatim, preserving the business
        content of unknown local escapes across the round trip.

    :rtype: str
    """
    esc = str(container.esc)
    rv = []
    for fragment in fragments:
        if fragment.kind == Fragment.TEXT:
            rv.append(escape(container, fragment.value, app_map))
        elif fragment.kind in (Fragment.FORMAT, Fragment.LOCAL):
            rv.append(esc + fragment.value + esc)
        else:
            raise ValueError("Unknown fragment kind {0!r}".format(fragment.kind))
    return "".join(rv)


def unescape(container, field, app_map=None):  # noqa: C901
    """
    See: http://www.hl7standards.com/blog/2006/11/02/hl7-escape-sequences/

    To process this correctly, the full set of separators (MSH.1/MSH.2) needs to be known.

    This will convert the identifiable sequences.
    If the application provides mapping, these are also used.
    Items which cannot be mapped are removed

    For example, the App Map count provide N, H, Zxxx values

    Chapter 2: Section 2.10

    At the moment, this functionality can:

    *   replace the parsing characters (2.10.4)
    *   replace highlight characters (2.10.3)
    *   replace hex characters. (2.10.5)
    *   replace rich text characters (2.10.6)
    *   replace application defined characters (2.10.7)

    It cannot:

    *   switch code pages / ISO IR character sets
    """
    if not field or field.find(container.esc) == -1:
        return field

    DEFAULT_MAP = {
        "H": "_",  # Override using the APP MAP: 2.10.3
        "N": "_",  # Override using the APP MAP
        "F": container.separators[1],  # 2.10.4
        "R": container.separators[2],
        "S": container.separators[3],
        "T": container.separators[4],
        "E": container.esc,
        ".br": "\r",  # 2.10.6
        ".sp": "\r",
        ".fi": "",
        ".nf": "",
        ".in": "    ",
        ".ti": "    ",
        ".sk": " ",
        ".ce": "\r",
    }

    rv = []
    collecting = []
    in_seq = False
    for offset, c in enumerate(field):
        if in_seq:
            if c == container.esc:
                in_seq = False
                value = "".join(collecting)
                collecting = []
                if not value:
                    logger.warning(
                        "Error unescaping value [%s], empty sequence found at %d",
                        field,
                        offset,
                    )
                    continue
                if app_map and value in app_map:
                    rv.append(app_map[value])
                elif value in DEFAULT_MAP:
                    rv.append(DEFAULT_MAP[value])
                elif value.startswith(".") and (
                    (app_map and value[:3] in app_map) or value[:3] in DEFAULT_MAP
                ):
                    # Substitution with a number of repetitions defined (2.10.6)
                    if app_map and value[:3] in app_map:
                        ch = app_map[value[:3]]
                    else:
                        ch = DEFAULT_MAP[value[:3]]
                    count = int(value[3:])
                    rv.append(ch * count)

                elif (
                    value[0] == "C"
                ):  # Convert to new Single Byte character set : 2.10.2
                    # Two HEX values, first value chooses the character set (ISO-IR), second gives the value
                    logger.warning(
                        "Error inline character sets [%s] not implemented, field [%s], offset [%s]",
                        value,
                        field,
                        offset,
                    )
                elif value[0] == "M":  # Switch to new Multi Byte character set : 2.10.2
                    # Three HEX values, first value chooses the character set (ISO-IR), rest give the value
                    logger.warning(
                        "Error inline character sets [%s] not implemented, field [%s], offset [%s]",
                        value,
                        field,
                        offset,
                    )
                elif value[0] == "X":  # Hex encoded Bytes: 2.10.5
                    value = value[1:]
                    try:
                        data = bytes(
                            int(value[off : off + 2], 16)
                            for off in range(0, len(value), 2)
                        )
                    except Exception:
                        logger.exception(
                            "Error decoding hex value [%s], field [%s], offset [%s]",
                            value,
                            field,
                            offset,
                        )
                    else:
                        try:
                            rv.append(data.decode("utf-8"))
                        except UnicodeDecodeError:
                            # Single byte escapes (e.g. \Xe1\) are latin-1
                            rv.append(data.decode("latin-1"))
                else:
                    logger.exception(
                        "Error decoding value [%s], field [%s], offset [%s]",
                        value,
                        field,
                        offset,
                    )
            else:
                collecting.append(c)
        elif c == container.esc:
            in_seq = True
        else:
            rv.append(str(c))

    return "".join(rv)
