import sys
import os
import subprocess
import argparse
from colorama import Fore, Style
from datetime import datetime
from pathlib import Path


# General settings:
EXTENSIONS = ('.mp4', '.avi', '.mov', '.mkv', '.wmv', '.mpg', '.3gp', '.m4v')
EXCLUSIONS = [' - x265.', ' - h265.', ' - av1.', 'no_recode.']

# Encoder parameters
FFMPEG_PARAMS_COMMON = "-stats -v error -y -hide_banner"
MAP_PARAMS_COMMON = "-map 0 -c copy"
ENCODER_PARAMS_AV1 = "-c:V:0 libsvtav1 -preset 7 -crf 34 -svtav1-params keyint=10s:scd=1:enable-qm=1:tune=0"  # abt 2 times speed of x265 with abt same quality and size


ENCODER_PARAMS_X265 = "-c:V:0 libx265 -preset medium -crf 26 -x265-params --no-sao=1:me=3:ref=5:b-adapt=2:weightb=1:log-level=error"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "64k"
MAX_RATIO = 85  # Result should be this % or less of original to be deleted
SIZE_TRESHOLD = 1.5  # How much times can AV1 be bigger than x265

os.environ["SVT_LOG"] = "1"
# typedef enum {
#    SVT_LOG_ALL   = -1,
#    SVT_LOG_FATAL = 0,
#    SVT_LOG_ERROR = 1,
#    SVT_LOG_WARN  = 2,
#    SVT_LOG_INFO  = 3,
#    SVT_LOG_DEBUG = 4,
# } SvtLogLevel;


def set_params_from_cmd():

    parser = argparse.ArgumentParser(prog='AllRecoder', description='Recode all videos in folder and subfolders selecting best codec', epilog="", add_help=False, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-d', '--delete', action='store_true', help="Delete file after successfull recode, if it's size is less than --ratio of original")
    parser.add_argument('-c', '--codec', choices=['best', 'x265', 'av1'], default='best', type=str, help="Force codec, or select best of two. x265 selected if it's size is --treshold times less than AV1")
    parser.add_argument('-r', '--ratio', type=int, default=MAX_RATIO, help="Maximum ratio for file to be deleted")
    parser.add_argument('-t', '--treshold', type=float, default=SIZE_TRESHOLD, help="How much times can AV1 be bigger than x265 to be still selected")
    parser.add_argument('-h', '--help', action='help', default=argparse.SUPPRESS, help='Show this help message and exit.')
    return parser.parse_args()


def find_video_files(directory):
    """Find all video files in the directory and subdirectories with EXTENSIONS, excepting EXCLUSIONS."""

    video_files = []
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.lower().endswith(EXTENSIONS) and not any(x in file.lower() for x in EXCLUSIONS):
                video_files.append(os.path.join(root, file))
    return video_files


def encode_to_av1(input_file):
    """Encode 30 seconds of the input file to AV1 using libsvtav1."""

    output_file = f"{input_file}-av1.mp4"
    command = f"ffmpeg {FFMPEG_PARAMS_COMMON} -i \"{input_file}\" {ENCODER_PARAMS_AV1} -t 30 -c:a {AUDIO_CODEC} -b:a {AUDIO_BITRATE} \"{output_file}\""
    subprocess.run(command, shell=True, creationflags=subprocess.IDLE_PRIORITY_CLASS)
    return output_file


def encode_to_h265(input_file):
    """Encode 30 seconds of the input file to H.265."""

    output_file = f"{input_file}-x265.mp4"
    command = f"ffmpeg {FFMPEG_PARAMS_COMMON} -i \"{input_file}\" {ENCODER_PARAMS_X265} -t 30 -c:a {AUDIO_CODEC} -b:a {AUDIO_BITRATE} \"{output_file}\""
    subprocess.run(command, shell=True, creationflags=subprocess.IDLE_PRIORITY_CLASS)
    return output_file


def compare_sizes(av1_file, h265_file, treshold):
    """Compare the sizes of the two encoded files and return the smaller one."""

    av1_size = os.path.getsize(av1_file)
    h265_size = os.path.getsize(h265_file)
    print(f"{Fore.GREEN}av1 version is {av1_size/1024/1024: .2f}Mb")
    print(f"{Fore.GREEN}h265 version is {h265_size/1024/1024: .2f}Mb")
    print(f"{Fore.GREEN}AV1/H265: {av1_size/h265_size:.2f}")

    if av1_size / h265_size <= treshold:
        return "av1", av1_size
    else:
        return "h265", h265_size


def get_video_info(input_file):
    """Returns duration of input video with microseconds truncated to 3"""

    command = f"ffprobe -v error -pretty -select_streams V:0 -show_entries stream=duration -show_entries stream=nb_frames -of default=nokey=1:noprint_wrappers=1 \"{input_file}\""
    output = subprocess.getoutput(command).splitlines()  # [0]: time; #[1]: frames
    dur = datetime.strptime(output[0], "%H:%M:%S.%f")
    if dur.hour == 0:
        duration = datetime.strftime(dur, "%M:%S.{}".format(str(dur.microsecond)[:2]))
    else:
        duration = datetime.strftime(dur, "%H:%M:%S.{}".format(str(dur.microsecond)[:2]))

    return duration, f"{int(output[1]):_d}".replace("_", "\u202F")


def remove_files(*files):
    """Remove the specified files."""

    for file in files:
        os.remove(file)


def encode_source_file(input_file, codec):
    """Encode the source file with the specified codec."""

    p = Path(input_file)
    output_file = f"{p.parent}\\{p.stem} - {codec.upper()}.mp4"

    if codec == "av1":
        command = f"ffmpeg {FFMPEG_PARAMS_COMMON} -i \"{input_file}\" {MAP_PARAMS_COMMON} {ENCODER_PARAMS_AV1} -c:a {AUDIO_CODEC} -b:a {AUDIO_BITRATE} \"{output_file}\""
    else:
        command = f"ffmpeg {FFMPEG_PARAMS_COMMON} -i \"{input_file}\" {MAP_PARAMS_COMMON} {ENCODER_PARAMS_X265} -c:a {AUDIO_CODEC} -b:a {AUDIO_BITRATE} \"{output_file}\""

    result = subprocess.run(command, shell=True, creationflags=subprocess.IDLE_PRIORITY_CLASS)
    return output_file, result.returncode


def main():
    args = set_params_from_cmd()

    print(f"""Codec:    {args.codec}
Delete:   {args.delete}
Ratio:    {args.ratio}
Treshold: {args.treshold}""")

    directory = "."  # Current directory
    video_files = find_video_files(directory)

    for input_file in video_files:
        print("-----")
        command = f"ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of default=noprint_wrappers=1:nokey=1 \"{input_file}\""
        input_codec = subprocess.getoutput(command)

        match input_codec.lower():
            case "av1" | "hevc":
                print(f"{input_file} is encoded with {input_codec} - skipping.")
                continue

        print(f"{Fore.GREEN}{datetime.now().strftime('%H:%M:%S')}: recoding \"{input_file}\"")

        if args.codec == 'best':
            print(f"{Fore.GREEN}Encoding 30secs to AV1...{Style.RESET_ALL}")
            av1_file = encode_to_av1(input_file)
            print(f"{Fore.GREEN}Encoding 30secs to h265...{Style.RESET_ALL}")
            h265_file = encode_to_h265(input_file)

            best_codec, _ = compare_sizes(av1_file, h265_file, args.treshold)

            remove_files(av1_file, h265_file)
        else:
            best_codec = args.codec

        print(f"{Fore.GREEN}{datetime.now().strftime('%H:%M:%S')}: Encoding source file with {Fore.RED}{best_codec}{Style.RESET_ALL}")
        video_duration, video_frames = get_video_info(input_file)
        print(f"{Fore.GREEN}Video duration: {Fore.YELLOW}{video_duration} / {video_frames} {Fore.GREEN}frames{Style.RESET_ALL}")
        start_time = datetime.now()
        output_file, returncode = encode_source_file(input_file, best_codec)
        if returncode == 0:

            elapsed = datetime.strptime(str(datetime.now()-start_time), "%H:%M:%S.%f")
            if elapsed.hour == 0:
                print(f"{Fore.YELLOW}Elapsed time: "+datetime.strftime(elapsed, "%M:%S.{}".format(str(elapsed.microsecond)[:2])))
            else:
                print(f"{Fore.YELLOW}Elapsed time: "+datetime.strftime(elapsed, "%H:%M:%S.{}".format(str(elapsed.microsecond)[:2])))

            original_size = os.path.getsize(input_file)
            encoded_size = os.path.getsize(output_file)
            size_ratio = (encoded_size / original_size) * 100

            print(f"{Fore.GREEN}{datetime.now().strftime('%H:%M:%S')}: Encoded {input_file} with {Fore.RED}{best_codec}{Style.RESET_ALL}.")
            print(f"{Fore.GREEN}Original size: {original_size / (1024 * 1024):.2f} MB, Encoded size: {encoded_size / (1024 * 1024):.2f} MB, Size ratio: {size_ratio:.2f}%{Style.RESET_ALL}")

            if args.delete:
                if size_ratio <= args.ratio:
                    os.remove(input_file)
                    print(f"{Fore.RED}Removed original file {input_file} as the encoded file is {args.ratio}% or less of the original size.{Style.RESET_ALL}")
                else:
                    os.remove(output_file)
                    p = Path(input_file)
                    output_file = f"{p.parent}\\{p.stem} - NO_RECODE{p.suffix}"
                    os.rename(input_file, output_file)
                    print(f"{Fore.RED}Renamed original file {input_file} as the encoded file is more than {args.ratio}% of the original size.{Style.RESET_ALL}")
        else:
            os.remove(output_file)
            print(f"{Fore.RED}{datetime.now().strftime('%H:%M:%S')}: Recoding process returned error!{Style.RESET_ALL}")

        print(f"{Style.RESET_ALL}")
    # end input_file in video_files:


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
