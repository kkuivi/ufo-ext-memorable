"""The assistant pack plus Memorable.

A stock UFO assistant pack does not name this extension. Selecting
`assistant-memorable` in `[pack] name` keeps that pack's extensions and adds this one.
"""

import ufo_pack_assistant as assistant

from ufo.sdk.manifest import Pack

NAME = "assistant-memorable"
VERSION = "0.1.0"


def pack() -> Pack:
    base = assistant.pack()
    extensions = tuple(dict.fromkeys((*base.extensions, "memorable")))
    return Pack(name=NAME, version=VERSION, extensions=extensions)
