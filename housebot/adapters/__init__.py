"""Registry of source adapters. The keys match the names under `sources:` in config.yaml.
"""

from .property24 import Property24
from .privateproperty import PrivateProperty

ADAPTERS = {a.name: a for a in (Property24, PrivateProperty)}
