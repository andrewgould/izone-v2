#!/bin/sh
# Render the brand images Home Assistant and HACS show for this integration
# (HA 2026.3+ reads them from custom_components/izone_v2/brand/). Sizes follow
# the home-assistant/brands spec: icons 256/512 square, logos 128/256 tall.
# Needs rsvg-convert (brew install librsvg); the logos use Avenir Next.
set -e
cd "$(dirname "$0")"
out=../../custom_components/izone_v2/brand
rsvg-convert -w 256 -h 256 icon.svg -o "$out/icon.png"
rsvg-convert -w 512 -h 512 icon.svg -o "$out/icon@2x.png"
rsvg-convert -h 128 logo.svg -o "$out/logo.png"
rsvg-convert -h 256 logo.svg -o "$out/logo@2x.png"
rsvg-convert -h 128 dark_logo.svg -o "$out/dark_logo.png"
rsvg-convert -h 256 dark_logo.svg -o "$out/dark_logo@2x.png"
