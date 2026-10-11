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

viewcode_follow_imported_members = False

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
# the way back out: the repo button in the sidebar, PyPI and the repo in the footer
html_theme_options = {
    "source_repository": "https://github.com/vntcore/PGPCbot/",
    "source_branch": "main",
    "source_directory": "docs/",
    "footer_icons": [
        {
            "name": "GitHub",
            "url": "https://github.com/vntcore/PGPCbot",
            "html": '<svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor"><path d="M8 0a8 8 0 0 0-2.53 15.59c.4.07.55-.17.55-.38v-1.34c-2.23.48-2.7-1.07-2.7-1.07-.36-.93-.89-1.18-.89-1.18-.73-.5.05-.49.05-.49.8.06 1.23.83 1.23.83.72 1.23 1.88.87 2.34.67.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.6 7.6 0 0 1 4 0c1.53-1.03 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.28.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48v2.2c0 .21.14.46.55.38A8 8 0 0 0 8 0Z"/></svg>',
            "class": "",
        },
        {
            "name": "PyPI",
            "url": "https://pypi.org/project/PGPCbot/",
            "html": '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M12 1.5 3 6v12l9 4.5 9-4.5V6l-9-4.5Zm0 2.3 6.6 3.3L12 10.4 5.4 7.1 12 3.8ZM5 8.8l6 3v7.6l-6-3V8.8Zm8 10.6V11.8l6-3v7.6l-6 3Z"/></svg>',
            "class": "",
        },
    ],
}

# copying an example leaves the prompts behind
copybutton_prompt_text = r">>> |\.\.\. |\$ "
copybutton_prompt_is_regexp = True
