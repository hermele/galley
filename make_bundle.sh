#!/bin/bash
# Build the zip bundle of a galley release: exactly the files of a Git commit or tag (default:
# HEAD), in a folder named galley/. Attach it to the GitHub Release of that tag.
# Part of galley by Michael Hermele. Copyright (C) 2026 The Regents of the University of Colorado.
# SPDX-License-Identifier: AGPL-3.0-or-later (with the additional term in NOTICE); see LICENSE.
# Usage: ./make_bundle.sh [tag-or-commit] [output-folder]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
rev="${1:-HEAD}"
out="$(cd "${2:-.}" && pwd)"
cd "$here"
version="$(git show "$rev:galley_server.py" | sed -n 's/^VERSION = "\(.*\)"$/\1/p')"
[[ -n "$version" ]] || { echo "no VERSION in galley_server.py at $rev" >&2; exit 1; }
if [[ "$rev" == v* && "$rev" != "v$version" ]]; then
  echo "tag $rev does not match VERSION $version" >&2; exit 1
fi
zip="$out/galley-$version.zip"
git archive --format=zip --prefix=galley/ -o "$zip" "$rev"
echo "$zip"
