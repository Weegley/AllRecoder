#!/usr/bin/env python3
import argparse
import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import tempfile
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init()
except Exception:  # colorama is optional
    class _Dummy:
        RED = GREEN = YELLOW = CYAN = ""
        RESET_ALL = ""
    Fore = Style = _Dummy()


EXTENSIONS = ('.mp4', '.avi', '.mov', '.mkv', '.wmv', '.mpg', '.mpeg', '.3gp', '.m4v')
EXCLUSIONS = [' - x265.', ' - h265.', ' - av1.', 'no_recode.']
FFMPEG_PARAMS_COMMON = ["-stats", "-v", "error", "-y", "-hide_banner"]
MAP_PARAMS_COMMON = ["-map", "0", "-dn", "-c", "copy"]
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "64k"
MAX_RATIO = 85
SIZE_THRESHOLD = 1.3
SAMPLE_SECONDS = 30
STALE_LOCK_SECONDS = 6 * 3600
HEARTBEAT_SECONDS = 60
IDLE_SLEEP_SECONDS = 20

# Keep SVT-AV1 logs quiet-ish.
os.environ.setdefault("SVT_LOG", "1")


@dataclass
class EncoderChoice:
    av1_encoder: Optional[str]
    ffmpeg_bin: str
    ffprobe_bin: str


class ActiveCleanup:
    def __init__(self):
        self._lock = threading.Lock()
        self._paths: set[Path] = set()
        self._file_lock: Optional['FileLock'] = None

    def set_lock(self, file_lock: Optional['FileLock']) -> None:
        with self._lock:
            self._file_lock = file_lock

    def add_path(self, path: Path) -> None:
        with self._lock:
            self._paths.add(Path(path))

    def discard_path(self, path: Path) -> None:
        with self._lock:
            self._paths.discard(Path(path))

    def cleanup(self) -> None:
        with self._lock:
            paths = sorted(self._paths, key=lambda p: len(str(p)), reverse=True)
            file_lock = self._file_lock
            self._paths.clear()
            self._file_lock = None

        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass

        if file_lock is not None:
            try:
                file_lock.release()
            except Exception:
                pass


ACTIVE_CLEANUP = ActiveCleanup()
atexit.register(ACTIVE_CLEANUP.cleanup)


def now_str() -> str:
    return datetime.now().strftime('%H:%M:%S')


def log(msg: str, color: str = "") -> None:
    reset = getattr(Style, "RESET_ALL", "") if color else ""
    print(f"{color}{msg}{reset}")


def format_elapsed(seconds: float) -> str:
    total_ms = int(seconds * 100)
    cs = total_ms % 100
    total_sec = total_ms // 100
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}.{cs:02d}"
    return f"{m:02d}:{s:02d}.{cs:02d}"


def format_duration(seconds: float) -> str:
    return format_elapsed(seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='AllRecoder',
        description='Cross-platform bulk video recoder with shared-folder locking for multiple machines.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('directory', nargs='?', default='.', help='Root folder to scan')
    parser.add_argument('-d', '--delete', action='store_true',
                        help='Delete source file after successful recode if encoded size is <= --ratio')
    parser.add_argument('-c', '--codec', choices=['best', 'x265', 'av1'], default='best',
                        help='Force codec, or compare 30-second AV1 and x265 samples and pick the best')
    parser.add_argument('-r', '--ratio', type=int, default=MAX_RATIO,
                        help='Maximum ratio for deleting source file')
    parser.add_argument('-t', '--threshold', '--treshold', dest='threshold', type=float, default=SIZE_THRESHOLD,
                        help='How many times AV1 may be bigger than x265 and still be selected')
    parser.add_argument('--sample-seconds', type=int, default=SAMPLE_SECONDS,
                        help='Length of AV1/x265 test encodes when --codec best')
    parser.add_argument('--watch', action='store_true',
                        help='Keep scanning the folder and processing new files as a worker')
    parser.add_argument('--idle-sleep', type=int, default=IDLE_SLEEP_SECONDS,
                        help='Sleep between scans in --watch mode when there is no work')
    parser.add_argument('--lock-ttl', type=int, default=STALE_LOCK_SECONDS,
                        help='Consider a lock stale after this many seconds')
    parser.add_argument('--heartbeat', type=int, default=HEARTBEAT_SECONDS,
                        help='How often to refresh the lock mtime while encoding')
    parser.add_argument('--worker-id', default=f"{socket.gethostname()}-{os.getpid()}",
                        help='Human-readable worker ID stored in lock files')
    parser.add_argument('--ffmpeg-bin', default='ffmpeg', help='Path to ffmpeg')
    parser.add_argument('--ffprobe-bin', default='ffprobe', help='Path to ffprobe')
    parser.add_argument('--av1-encoder', choices=['auto', 'libsvtav1', 'libaom-av1'], default='auto',
                        help='AV1 encoder to use when AV1 is requested')
    return parser


class LockHeartbeat:
    def __init__(self, lock_path: Path, interval: int):
        self.lock_path = lock_path
        self.interval = max(5, interval)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                now = time.time()
                os.utime(self.lock_path, (now, now))
            except FileNotFoundError:
                return
            except Exception:
                # If the lock cannot be touched for some reason, keep trying.
                pass

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1.0)


class FileLock:
    def __init__(self, source_file: Path, worker_id: str, ttl_seconds: int, heartbeat_seconds: int):
        self.source_file = source_file
        self.lock_path = source_file.with_name(source_file.name + '.allrecoder.lock')
        self.worker_id = worker_id
        self.ttl_seconds = ttl_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.heartbeat: Optional[LockHeartbeat] = None
        self.acquired = False

    def acquire(self) -> bool:
        payload = {
            'worker_id': self.worker_id,
            'hostname': socket.gethostname(),
            'pid': os.getpid(),
            'source_file': str(self.source_file),
            'started_at': datetime.now().isoformat(timespec='seconds'),
        }
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode('utf-8')

        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, 'wb') as f:
                    f.write(body)
                self.acquired = True
                ACTIVE_CLEANUP.set_lock(self)
                self.heartbeat = LockHeartbeat(self.lock_path, self.heartbeat_seconds)
                self.heartbeat.start()
                return True
            except FileExistsError:
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age <= self.ttl_seconds:
                    return False
                log(f"{now_str()}: stale lock detected for '{self.source_file.name}', removing it.", Fore.YELLOW)
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    continue
                except PermissionError:
                    return False
            except Exception as exc:
                log(f"{now_str()}: failed to create lock '{self.lock_path}': {exc}", Fore.RED)
                return False

    def release(self) -> None:
        if self.heartbeat:
            self.heartbeat.stop()
        if self.acquired:
            try:
                self.lock_path.unlink(missing_ok=True)
            except Exception:
                pass
        ACTIVE_CLEANUP.set_lock(None)
        self.acquired = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError('lock_not_acquired')
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


def ensure_tool(path_or_name: str) -> str:
    resolved = shutil.which(path_or_name) if not Path(path_or_name).exists() else str(Path(path_or_name))
    if not resolved:
        raise FileNotFoundError(f"Tool not found: {path_or_name}")
    return resolved


def run_command(args: Sequence[str], *, lower_priority: bool = True) -> subprocess.CompletedProcess:
    kwargs = {'check': False}
    if os.name == 'nt':
        creationflags = 0
        if lower_priority and hasattr(subprocess, 'IDLE_PRIORITY_CLASS'):
            creationflags = subprocess.IDLE_PRIORITY_CLASS
        kwargs['creationflags'] = creationflags
    else:
        if lower_priority:
            def _preexec() -> None:
                try:
                    os.nice(10)
                except Exception:
                    pass
            kwargs['preexec_fn'] = _preexec
    return subprocess.run(list(args), **kwargs)


def get_output(args: Sequence[str]) -> str:
    completed = subprocess.run(list(args), capture_output=True, text=True, check=False)
    return (completed.stdout or '').strip()


def has_encoder(ffmpeg_bin: str, encoder_name: str) -> bool:
    completed = subprocess.run(
        [ffmpeg_bin, '-hide_banner', '-encoders'],
        capture_output=True,
        text=True,
        check=False,
    )
    output = completed.stdout or ''
    return encoder_name in output


def choose_av1_encoder(args: argparse.Namespace) -> EncoderChoice:
    ffmpeg_bin = ensure_tool(args.ffmpeg_bin)
    ffprobe_bin = ensure_tool(args.ffprobe_bin)

    av1_encoder = None
    if args.av1_encoder == 'libsvtav1':
        if not has_encoder(ffmpeg_bin, 'libsvtav1'):
            raise RuntimeError('ffmpeg does not support libsvtav1')
        av1_encoder = 'libsvtav1'
    elif args.av1_encoder == 'libaom-av1':
        if not has_encoder(ffmpeg_bin, 'libaom-av1'):
            raise RuntimeError('ffmpeg does not support libaom-av1')
        av1_encoder = 'libaom-av1'
    else:
        if has_encoder(ffmpeg_bin, 'libsvtav1'):
            av1_encoder = 'libsvtav1'
        elif has_encoder(ffmpeg_bin, 'libaom-av1'):
            av1_encoder = 'libaom-av1'

    if not has_encoder(ffmpeg_bin, 'libx265'):
        raise RuntimeError('ffmpeg does not support libx265')

    return EncoderChoice(av1_encoder=av1_encoder, ffmpeg_bin=ffmpeg_bin, ffprobe_bin=ffprobe_bin)


def is_service_or_temp_file(path: Path) -> bool:
    name = path.name.lower()
    if path.name.startswith('.'):
        return True
    if name.endswith('.allrecoder.lock'):
        return True
    if '.allrecoder.' in name:
        return True
    if '.libsvtav1.' in name or '.libaom_av1.' in name or '.x265.' in name:
        return True
    return False


def list_video_files(directory: Path) -> List[Path]:
    files: List[Path] = []
    for path in directory.rglob('*'):
        if not path.is_file():
            continue
        if is_service_or_temp_file(path):
            continue
        name = path.name.lower()
        if not name.endswith(EXTENSIONS):
            continue
        if any(x in name for x in EXCLUSIONS):
            continue
        files.append(path)
    return sorted(files)


def build_av1_params(encoder_name: str) -> List[str]:
    if encoder_name == 'libsvtav1':
        return [
            '-c:v:0', 'libsvtav1',
            '-preset', '7',
            '-crf', '35',
            '-svtav1-params', 'keyint=10s:scd=1:enable-qm=1:tune=0',
        ]
    if encoder_name == 'libaom-av1':
        return [
            '-c:v:0', 'libaom-av1',
            '-cpu-used', '6',
            '-crf', '35',
            '-b:v', '0',
            '-row-mt', '1',
        ]
    raise RuntimeError('No AV1 encoder available')


def build_x265_params() -> List[str]:
    return [
        '-c:v:0', 'libx265',
        '-preset', 'medium',
        '-crf', '26',
        '-x265-params', 'no-sao=1:me=3:ref=5:b-adapt=2:weightb=1:log-level=error',
    ]


def make_sample_path(input_file: Path, suffix: str, worker_id: str) -> Path:
    safe_worker = ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in worker_id)
    source_tag = hashlib.sha1(str(input_file).encode('utf-8', errors='ignore')).hexdigest()[:10]
    temp_dir = Path(tempfile.gettempdir()) / 'allrecoder_samples'
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir / f"{input_file.stem}.{source_tag}.{safe_worker}.{os.getpid()}.{suffix}.mp4"


def encode_sample(ffmpeg_bin: str, input_file: Path, codec: str, av1_encoder: Optional[str],
                  sample_seconds: int, worker_id: str) -> Path:
    if codec == 'av1':
        if not av1_encoder:
            raise RuntimeError('AV1 requested but no AV1 encoder is available')
        params = build_av1_params(av1_encoder)
        output_file = make_sample_path(input_file, av1_encoder.replace('-', '_'), worker_id)
    else:
        params = build_x265_params()
        output_file = make_sample_path(input_file, 'x265', worker_id)

    cmd = [
        ffmpeg_bin, *FFMPEG_PARAMS_COMMON,
        '-i', str(input_file),
        *params,
        '-t', str(sample_seconds),
        '-c:a', AUDIO_CODEC,
        '-b:a', AUDIO_BITRATE,
        str(output_file),
    ]
    ACTIVE_CLEANUP.add_path(output_file)
    try:
        result = run_command(cmd)
    except KeyboardInterrupt:
        raise
    except Exception:
        remove_files(output_file)
        ACTIVE_CLEANUP.discard_path(output_file)
        raise
    if result.returncode != 0 or not output_file.exists():
        remove_files(output_file)
        ACTIVE_CLEANUP.discard_path(output_file)
        raise RuntimeError(f"Test encode failed for {codec}: {input_file.name}")
    return output_file


def compare_sizes(av1_file: Path, h265_file: Path, threshold: float) -> Tuple[str, int]:
    av1_size = av1_file.stat().st_size
    h265_size = h265_file.stat().st_size
    log(f"AV1 version is {av1_size / 1024 / 1024:.2f} MB", Fore.GREEN)
    log(f"H265 version is {h265_size / 1024 / 1024:.2f} MB", Fore.GREEN)
    log(f"AV1/H265: {av1_size / h265_size:.2f}", Fore.GREEN)
    if av1_size / h265_size <= threshold:
        return 'av1', av1_size
    return 'x265', h265_size


def get_video_codec(ffprobe_bin: str, input_file: Path) -> str:
    output = get_output([
        ffprobe_bin, '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=codec_name',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(input_file),
    ])
    return (output or '').strip().lower()


def get_video_info(ffprobe_bin: str, input_file: Path) -> Tuple[str, str]:
    duration_output = get_output([
        ffprobe_bin, '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(input_file),
    ])
    frames_output = get_output([
        ffprobe_bin, '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=nb_frames',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(input_file),
    ])

    try:
        duration = format_duration(float(duration_output))
    except Exception:
        duration = 'unknown'

    try:
        frames = f"{int(frames_output):_d}".replace('_', ' ')
    except Exception:
        frames = 'unknown'

    return duration, frames


def remove_files(*files: Path) -> None:
    for file in files:
        try:
            file.unlink(missing_ok=True)
        except Exception:
            pass


def build_output_path(input_file: Path, codec: str) -> Path:
    return input_file.with_name(f"{input_file.stem} - {codec.upper()}.mp4")


def encode_source_file(ffmpeg_bin: str, input_file: Path, codec: str, av1_encoder: Optional[str]) -> Tuple[Path, int]:
    output_file = build_output_path(input_file, codec)
    if codec == 'av1':
        if not av1_encoder:
            raise RuntimeError('AV1 requested but no AV1 encoder is available')
        params = build_av1_params(av1_encoder)
    else:
        params = build_x265_params()

    cmd = [
        ffmpeg_bin, *FFMPEG_PARAMS_COMMON,
        '-i', str(input_file),
        *MAP_PARAMS_COMMON,
        *params,
        '-c:a', AUDIO_CODEC,
        '-b:a', AUDIO_BITRATE,
        str(output_file),
    ]
    ACTIVE_CLEANUP.add_path(output_file)
    try:
        result = run_command(cmd)
    except KeyboardInterrupt:
        raise
    except Exception:
        remove_files(output_file)
        ACTIVE_CLEANUP.discard_path(output_file)
        raise
    return output_file, result.returncode


def process_file(input_file: Path, args: argparse.Namespace, tools: EncoderChoice) -> bool:
    lock = FileLock(input_file, args.worker_id, args.lock_ttl, args.heartbeat)
    if not lock.acquire():
        return False

    try:
        if not input_file.exists():
            return False

        input_codec = get_video_codec(tools.ffprobe_bin, input_file)
        if input_codec in ('av1', 'hevc'):
            log(f"{input_file} is encoded with {input_codec} - skipping.", Fore.YELLOW)
            return True

        best_codec = args.codec
        if args.codec == 'best':
            if not tools.av1_encoder:
                log(f"{now_str()}: no AV1 encoder found, falling back to x265 for '{input_file.name}'.", Fore.YELLOW)
                best_codec = 'x265'
            else:
                log(f"{now_str()}: recoding '{input_file}'", Fore.GREEN)
                log(f"Encoding {args.sample_seconds} sec to AV1 ({tools.av1_encoder})...", Fore.GREEN)
                av1_file = encode_sample(tools.ffmpeg_bin, input_file, 'av1', tools.av1_encoder,
                                         args.sample_seconds, args.worker_id)
                log(f"Encoding {args.sample_seconds} sec to x265...", Fore.GREEN)
                h265_file = encode_sample(tools.ffmpeg_bin, input_file, 'x265', tools.av1_encoder,
                                          args.sample_seconds, args.worker_id)
                try:
                    best_codec, _ = compare_sizes(av1_file, h265_file, args.threshold)
                finally:
                    remove_files(av1_file, h265_file)
                    ACTIVE_CLEANUP.discard_path(av1_file)
                    ACTIVE_CLEANUP.discard_path(h265_file)
        else:
            log(f"{now_str()}: recoding '{input_file}'", Fore.GREEN)

        video_duration, video_frames = get_video_info(tools.ffprobe_bin, input_file)
        log(f"{now_str()}: encoding source file with {best_codec.upper()}", Fore.GREEN)
        log(f"Video duration: {video_duration} / {video_frames} frames", Fore.YELLOW)

        start = time.monotonic()
        output_file, returncode = encode_source_file(tools.ffmpeg_bin, input_file, best_codec, tools.av1_encoder)
        elapsed = time.monotonic() - start

        if returncode != 0:
            remove_files(output_file)
            ACTIVE_CLEANUP.discard_path(output_file)
            log(f"{now_str()}: recoding returned an error for '{input_file.name}'.", Fore.RED)
            return True

        ACTIVE_CLEANUP.discard_path(output_file)
        original_size = input_file.stat().st_size
        encoded_size = output_file.stat().st_size
        size_ratio = (encoded_size / original_size) * 100

        log(f"Elapsed time: {format_elapsed(elapsed)}", Fore.YELLOW)
        log(f"{now_str()}: encoded '{input_file.name}' with {best_codec.upper()}.", Fore.GREEN)
        log(
            f"Original size: {original_size / (1024 * 1024):.2f} MB, "
            f"Encoded size: {encoded_size / (1024 * 1024):.2f} MB, "
            f"Size ratio: {size_ratio:.2f}%",
            Fore.GREEN,
        )

        if args.delete:
            if size_ratio <= args.ratio:
                input_file.unlink(missing_ok=True)
                log(f"Removed original file '{input_file}'.", Fore.RED)
            else:
                remove_files(output_file)
                ACTIVE_CLEANUP.discard_path(output_file)
                renamed = input_file.with_name(f"{input_file.stem} - NO_RECODE{input_file.suffix}")
                input_file.rename(renamed)
                log(
                    f"Renamed original file to '{renamed.name}' because encoded file is more than {args.ratio}% "
                    f"of the original size.",
                    Fore.RED,
                )
        return True
    finally:
        lock.release()


def run_once(args: argparse.Namespace, tools: EncoderChoice) -> bool:
    root = Path(args.directory).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Directory does not exist: {root}")

    video_files = list_video_files(root)
    processed_any = False
    locked_count = 0

    for input_file in video_files:
        processed = process_file(input_file, args, tools)
        if processed:
            processed_any = True
            print('-' * 60)
        else:
            locked_count += 1

    if locked_count and not processed_any:
        log(f"{now_str()}: all matching files are currently locked by other workers.", Fore.YELLOW)

    return processed_any


def main() -> int:
    args = build_parser().parse_args()
    tools = choose_av1_encoder(args)

    log(
        f"Codec: {args.codec}\n"
        f"Delete: {args.delete}\n"
        f"Ratio: {args.ratio}\n"
        f"Threshold: {args.threshold}\n"
        f"Sample seconds: {args.sample_seconds}\n"
        f"Worker ID: {args.worker_id}\n"
        f"FFmpeg: {tools.ffmpeg_bin}\n"
        f"FFprobe: {tools.ffprobe_bin}\n"
        f"AV1 encoder: {tools.av1_encoder or 'not available'}",
        Fore.CYAN,
    )

    if args.watch:
        log(f"Watch mode enabled. Scanning '{Path(args.directory).resolve()}' every {args.idle_sleep}s.", Fore.CYAN)
        while True:
            processed = run_once(args, tools)
            if not processed:
                time.sleep(max(1, args.idle_sleep))
    else:
        run_once(args, tools)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log(f"{now_str()}: interrupted, cleaning up active lock and temporary files...", Fore.YELLOW)
        ACTIVE_CLEANUP.cleanup()
        sys.exit(130)
    except Exception as exc:
        log(f"Fatal error: {exc}", Fore.RED)
        sys.exit(1)
