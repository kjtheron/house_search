"""Registry of source adapters. The keys match the names under `sources:` in config.yaml.
"""

from .pamgolding import PamGolding
from .privateproperty import PrivateProperty
from .propdata import Harcourts, Seeff
from .property24 import Property24

ADAPTERS = {a.name: a for a in (Property24, PrivateProperty, PamGolding, Seeff, Harcourts)}
