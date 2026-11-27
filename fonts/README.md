# Fonts

`scripts/build_cards.py` subsets these with `pyftsubset` and embeds only the glyphs each card uses.

| File | Used for | License |
| --- | --- | --- |
| `Inter-Bold.otf` | names, big numbers | SIL Open Font License 1.1 |
| `Inter-Regular.otf` | body text | SIL Open Font License 1.1 |
| `LiberationMono-Regular.ttf` | labels, code-style text | SIL Open Font License 1.1 |

To switch typefaces, drop the new files here and list them first in `FONT_CANDIDATES`
at the top of `scripts/build_cards.py`. The neon design was drawn with Space Grotesk
(display and body) and JetBrains Mono, and the config already looks for
`SpaceGrotesk-Bold.ttf`, `SpaceGrotesk-Regular.ttf` and `JetBrainsMono-Regular.ttf`
before falling back to the bundled fonts.
