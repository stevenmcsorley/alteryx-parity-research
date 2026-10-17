# Vendored third-party code

| Package | Version | Licence | Why vendored |
|---|---|---|---|
| `summa` (TextRank) | 1.2.0, https://github.com/summanlp/textrank | MIT (`summa/LICENSE`) | Published only as a source archive, and the bundled Windows runtime accepts wheels only. The only changes: absolute `summa` imports are now relative, and the command-line `textrank.py` is left out. |
