[![](https://github.com/Weegley/AllRecoder/actions/workflows/python-app.yml/badge.svg)](#)
# AllRecoder
Recode all files in folder and subfolders selecting from AV1 or x265 codec based on size and speed

## Dependencies:  
[FFmpeg, FFprobe](https://www.ffmpeg.org/)  
[Python 3](https://www.python.org/)

## Usage:  
```
usage: AllRecoder [-h] [-d] [-c {best,x265,av1}] [-r RATIO] [-t THRESHOLD] [--sample-seconds SAMPLE_SECONDS] [--watch] [--idle-sleep IDLE_SLEEP] [--lock-ttl LOCK_TTL]
                  [--heartbeat HEARTBEAT] [--worker-id WORKER_ID] [--ffmpeg-bin FFMPEG_BIN] [--ffprobe-bin FFPROBE_BIN] [--av1-encoder {auto,libsvtav1,libaom-av1}]
                  [directory]

Cross-platform bulk video recoder with shared-folder locking for multiple machines.

positional arguments:
  directory             Root folder to scan (default: .)

options:
  -h, --help            show this help message and exit
  -d, --delete          Delete source file after successful recode if encoded size is <= --ratio (default: False)
  -c, --codec {best,x265,av1}
                        Force codec, or compare 30-second AV1 and x265 samples and pick the best (default: best)
  -r, --ratio RATIO     Maximum ratio for deleting source file (default: 85)
  -t, --threshold, --treshold THRESHOLD
                        How many times AV1 may be bigger than x265 and still be selected (default: 1.3)
  --sample-seconds SAMPLE_SECONDS
                        Length of AV1/x265 test encodes when --codec best (default: 30)
  --watch               Keep scanning the folder and processing new files as a worker (default: False)
  --idle-sleep IDLE_SLEEP
                        Sleep between scans in --watch mode when there is no work (default: 20)
  --lock-ttl LOCK_TTL   Consider a lock stale after this many seconds (default: 21600)
  --heartbeat HEARTBEAT
                        How often to refresh the lock mtime while encoding (default: 60)
  --worker-id WORKER_ID
                        Human-readable worker ID stored in lock files (default: jonsbo-85390)
  --ffmpeg-bin FFMPEG_BIN
                        Path to ffmpeg (default: ffmpeg)
  --ffprobe-bin FFPROBE_BIN
                        Path to ffprobe (default: ffprobe)
  --av1-encoder {auto,libsvtav1,libaom-av1}
                        AV1 encoder to use when AV1 is requested (default: auto)

```
