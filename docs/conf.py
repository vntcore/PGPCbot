"""Sphinx configuration for the PGPCbot documentation.

    pip install -e . --group docs
    sphinx-build -b html docs docs/_build/html

then open docs/_build/html/index.html. The API reference is generated from the
docstrings in src/pgpcbot (Google style, read by napoleon); the guides are
Markdown, read by MyST.
"""

from __future__ import annotations

import sys
from pathlib import Path

# this checkout, not whatever copy is installed: the docs describe the code next to them
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pgpcbot import __version__

project = "PGPCbot"
author = "PGP Chat"
copyright = "2026, PGP Chat"
release = __version__
version = ".".join(__version__.split(".")[:2])

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "myst_parser",
    "sphinx_copybutton",
]

root_doc = "index"
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
language = "en"

# --- docstrings ---

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_rtype = False
napoleon_use_ivar = False
napoleon_attr_annotations = True

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_typehints_description_target = "documented_params"
autodoc_preserve_defaults = True
autodoc_class_signature = "mixed"
# the class docstring says what it is, the __init__ docstring lists the arguments
autoclass_content = "both"
add_module_names = False
python_use_unqualified_type_names = True
# Bot() has twenty arguments: one per line reads better than one long line
python_maximum_signature_line_length = 88
toc_object_entries_show_parents = "hide"

# `Name` in a docstring is a link to that object
default_role = "py:obj"

# every reference must resolve, except into libraries that publish no inventory
nitpicky = True
nitpick_ignore = [
    # the type variable of the decorators: "gives back the function it was given"
    ("py:class", "pgpcbot.router.F"),
]
nitpick_ignore_regex = [
    (r"py:.*", r"httpx2(\..*)?"),
    (r"py:.*", r"pysequoia(\..*)?"),
    (r"py:.*", r"openpgp(\..*)?"),
    (r"py:.*", r"rpgp_composed(\..*)?"),
]

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}
intersphinx_timeout = 10

# --- Markdown pages ---

myst_enable_extensions = ["colon_fence", "deflist"]
myst_heading_anchors = 3

# --- HTML ---

html_theme = "furo"
html_title = f"PGPCbot {release}"
html_short_title = "PGPCbot"
html_copy_source = False
html_show_sourcelink = False

# copying an example leaves the prompts behind
copybutton_prompt_text = r">>> |\.\.\. |\$ "
copybutton_prompt_is_regexp = True
