#!/usr/bin/env bash
# Sentinel-2 L2A from Element84 Earth Search: one 4-band GeoTIFF (B04 red, B03 green, B02 blue, B08 NIR) per date,
# all cropped to the same window, so all frames share one grid. Needs curl, jq and the GDAL command line tools.
#   scripts/data/fetch-s2.sh          -> $DATA/raw/$S2_NAME/<date>.tif (existing dates are skipped)
# Settings (bench.env): S2_NAME, S2_MGRS, S2_FROM, S2_TO, S2_MAXCLOUD, S2_WINDOW (lon/lat: ulx uly lrx lry).
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"

OUT="$DATA/raw/$S2_NAME"
FROM="$S2_FROM"; TO="$S2_TO"; MAXCLOUD="$S2_MAXCLOUD"
read -r ULX ULY LRX LRY <<< "$S2_WINDOW"
mkdir -p "$OUT"

# 1. search the catalogue (Element84 Earth Search, public)
curl -sf -X POST https://earth-search.aws.element84.com/v1/search \
  -H 'Content-Type: application/json' \
  -d "{\"collections\":[\"sentinel-2-l2a\"],
       \"datetime\":\"${FROM}T00:00:00Z/${TO}T23:59:59Z\",
       \"query\":{\"grid:code\":{\"eq\":\"MGRS-$S2_MGRS\"},\"eo:cloud_cover\":{\"lt\":$MAXCLOUD}},
       \"limit\":200}" > "$OUT/items.json"

# 2. one scene per date (if a date was processed twice, keep the newer processing), as a table
jq -r '.features
       | group_by(.properties.datetime[0:10]) | map(max_by(.id)) | .[]
       | [.properties.datetime[0:10], .properties."eo:cloud_cover",
          .assets.red.href, .assets.green.href, .assets.blue.href, .assets.nir.href] | @tsv' \
   "$OUT/items.json" > "$OUT/scenes.tsv"
echo "$(wc -l < "$OUT/scenes.tsv") scenes found"

# 3. per date: stack the 4 bands, crop the same window, write a GeoTIFF (only the needed parts are downloaded)
export GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif GDAL_HTTP_MAX_RETRY=3
while IFS=$'\t' read -r date cloud red green blue nir; do
  f="$OUT/$date.tif"
  [[ -e "$f" ]] && { echo "$date exists"; continue; }
  gdalbuildvrt -q -separate "$OUT/$date.vrt" "/vsicurl/$red" "/vsicurl/$green" "/vsicurl/$blue" "/vsicurl/$nir"
  gdal_translate -q -projwin "$ULX" "$ULY" "$LRX" "$LRY" -projwin_srs EPSG:4326 -a_nodata 0 \
                 -co COMPRESS=DEFLATE -co TILED=YES "$OUT/$date.vrt" "$f.tmp.tif"
  mv "$f.tmp.tif" "$f"; rm "$OUT/$date.vrt"
  echo "$date  cloud ${cloud}%  -> $f"
done < "$OUT/scenes.tsv"