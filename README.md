# AllRecoder
Recode all files in folder and subfolders selecting from AV1 or x265 codec based on size and speed

## Dependencies:  
[FFmpeg, FFprobe](https://www.ffmpeg.org/)  
[Python 3](https://www.python.org/)

## Usage:  
```
./AllRecoder [options]
options:  
  -d, --delete          Delete file after successfull recode, if it's size is less than --ratio of original (default: False)  
  -c, --codec {best,x265,av1}  
                        Force codec, or select best of two. x265 selected if it's size is --treshold times less than AV1 (default: best)  
  -r, --ratio RATIO     Maximum ratio for file to be deleted (default: 85)  
  -t, --treshold TRESHOLD  
                        How much times can AV1 be bigger than x265 to be still selected (default: 1.5)  
  -h, --help            Show this help message and exit.
```
