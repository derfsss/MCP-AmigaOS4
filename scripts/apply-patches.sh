#!/usr/bin/env bash
# apply-patches.sh — apply strategy-(b) patches to third-party submodules.
#
# For each `third-party/<name>-patches/` directory containing
# `*.patch` files, run `git apply` against the matching
# `third-party/<name>/` submodule. No-op when no patches exist
# (which is the phase-0 state).
#
# Idempotent in the sense that re-running on an already-patched tree
# fails noisily — the build system should clean the submodule
# (`git submodule foreach git reset --hard`) before re-applying.

set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo_root"

shopt -s nullglob
patched_any=0

for patch_dir in third-party/*-patches/; do
    name=$(basename "$patch_dir" -patches)
    target="third-party/$name"
    if [[ ! -d "$target" ]]; then
        echo "apply-patches: skipping $name — submodule not present at $target" >&2
        continue
    fi

    patches=( "$patch_dir"*.patch )
    if (( ${#patches[@]} == 0 )); then
        continue
    fi

    echo "apply-patches: $name (${#patches[@]} patch(es))"
    for p in "${patches[@]}"; do
        echo "  applying $(basename "$p")"
        git -C "$target" apply --whitespace=nowarn "$repo_root/$p"
    done
    patched_any=1
done

if (( patched_any == 0 )); then
    echo "apply-patches: no patches to apply"
fi
