#!/usr/bin/env bash
# Last week's release, for the build (which carries popular products and nutrition forward) and for verify
# (whose gates compare with it). Used by both jobs, so each checks the signature for itself.
#
# Downloads last week's index.json into ./previous and checks its signature with the current key, or during a
# key change the one before it (keys/previous-public.pem). Then downloads the one pack popular_gate.py picks as
# the baseline (the worldwide pack, or while changing over from country packs, the UK's). The pack itself is
# checked against the signed index's size and SHA-256 by popular_gate.open_pack before anything reads it.
#
# Required whenever a release exists, so the gates can't be skipped just because a download failed.
# Appends to $GITHUB_ENV:
#   PREVIOUS          --previous previous/index.json, or --first-release        (verify_packs.py)
#   PREVIOUS_PACK     --previous-index ... --previous-dir previous, or --first-release
#                                                     (build_packs.py and popular_gate.py)
set -euo pipefail

TAG=$(gh release list --limit 100 --json tagName,createdAt \
      --jq '[.[] | select(.tagName | startswith("packs-"))] | sort_by(.createdAt) | last | .tagName // empty')
if [ -z "$TAG" ]; then
  echo "No previous packs release: this is the first."
  echo "PREVIOUS=--first-release" >> "$GITHUB_ENV"
  echo "PREVIOUS_PACK=--first-release" >> "$GITHUB_ENV"
  exit 0
fi

gh release download "$TAG" --pattern index.json --pattern index.json.sig --dir previous
base64 -d previous/index.json.sig > previous/index.sig.bin
ok=no
for key in keys/food-pack-public.pem keys/previous-public.pem; do
  [ -f "$key" ] || continue
  if openssl pkeyutl -verify -pubin -inkey "$key" -rawin -in previous/index.json -sigfile previous/index.sig.bin; then
    ok=yes
    break
  fi
done
[ "$ok" = yes ] || { echo "Last week's index doesn't verify with the current or previous key"; exit 1; }

BASE=$(python3 -I scripts/popular_gate.py --pick-baseline previous/index.json)
[[ "$BASE" =~ ^[a-z0-9-]+\.sqlite\.deflate$ ]] || { echo "odd file name in last week's index"; exit 1; }
gh release download "$TAG" --pattern "$BASE" --dir previous

echo "PREVIOUS=--previous previous/index.json" >> "$GITHUB_ENV"
echo "PREVIOUS_PACK=--previous-index previous/index.json --previous-dir previous" >> "$GITHUB_ENV"
