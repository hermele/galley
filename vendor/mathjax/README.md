# MathJax 3.2.2 (included in galley)

galley renders mathematics with MathJax, included here so that galley works offline and its page
never loads scripts from other sites.

- **What:** the parts of MathJax 3.2.2 (es5 build) that galley loads: `tex-chtml-full.js`,
  `output/chtml/fonts/tex.js` and the fonts in `output/chtml/fonts/woff-v2/`, unchanged, in the
  same folder layout as in the MathJax distribution (`es5/`). `LICENSE` is MathJax's own.
- **Where from:** the npm package `mathjax@3.2.2`
  (`https://registry.npmjs.org/mathjax/-/mathjax-3.2.2.tgz`). The tarball matched the registry's
  integrity value (`sha512-Bt+SSVU8eBG27zChVewOicYs7Xsdt40qm4+UpHyX7k0/O9NliPc+x77k1/FEsPsjKPZGJvtRZM1vO+geW0OhGw==`),
  and every file here is byte-identical to the same file in the GitHub release tag
  `https://github.com/mathjax/MathJax/tree/3.2.2` (checked 2026-11-19).
- **Checksums:** `SHA256SUMS` (check with `sha256sum -c SHA256SUMS` or `shasum -a 256 -c SHA256SUMS`
  in this folder; galley's tests check it too).
- **License:** Apache License 2.0, Copyright The MathJax Consortium
  (https://www.mathjax.org/). See `LICENSE`.
