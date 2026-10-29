# Test fixtures

Two small tile stores written by mabench, read by `FormatSuite` to check that the Scala reader and writer match
mabench byte for byte. Regenerate after a change to the tile store format (from this directory):

```bash
rm -rf ramp normal
mabench synth --out ramp --width 50 --height 30 --generator ramp --bands 2 --param nodata_block=0,0,5,5 --tile-size 16
mabench synth --out normal --width 61 --height 47 --generator normal --bands 2 --frames 2 --seed 7 \
              --param nodata_fraction=0.2 --param nodata_block=10,10,20,40 --tile-size 16
```
