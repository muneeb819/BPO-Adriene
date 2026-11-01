"""Engine-specific TTS implementations, kept out of the TTS facade.

Each module holds one engine's synthesis code plus the metadata the API needs
about it. They are written as mixins: :class:`agent.tts.TTS` inherits them, so
the methods still see ``self.config``, ``self._promote`` and the rest of the
facade exactly as before, while :mod:`agent.tts` stops carrying every engine's
internals in one 1000-line file.

Adding an engine means adding a module here and listing its mixin in
:class:`agent.tts.TTS` — nothing else has to change.
"""
