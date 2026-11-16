from importlib.metadata import version

from ofxtools.Client import OFXClient
from ofxtools.migrate import (
    MigrationError,
    MigrationReport,
    MigrationResult,
    StructureError,
    convert,
    convert_file,
    verify,
)
from ofxtools.Parser import OFXTree

__version__ = version("ofxtools")
