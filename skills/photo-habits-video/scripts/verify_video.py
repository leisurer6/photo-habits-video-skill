#!/usr/bin/env python3
"""Decode every frame of a rendered video and record observable validation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def find_ffmpeg(explicit=None):
    candidate=explicit or os.environ.get('FFMPEG_BINARY') or shutil.which('ffmpeg')
    if candidate:
        return str(candidate)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError,RuntimeError) as exc:
        raise RuntimeError('FFmpeg not found. Supply --ffmpeg PATH or install imageio-ffmpeg.') from exc


def verify(video, ffmpeg=None, expected_duration=None, expected_fps=None,
           expected_size=None, require_audio=False):
    video=Path(video).resolve()
    if not video.is_file():
        raise ValueError(f'Video does not exist: {video}')
    with tempfile.TemporaryDirectory(prefix='photo-video-check-') as temporary:
        progress=Path(temporary)/'progress.txt'
        command=[find_ffmpeg(ffmpeg),'-hide_banner','-nostdin','-i',str(video),
                 '-map','0:v:0','-map','0:a?','-af','volumedetect',
                 '-progress',str(progress),'-f','null','-']
        process=subprocess.run(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                               text=True,encoding='utf-8',errors='replace')
        log=process.stderr
        events=progress.read_text(encoding='utf-8') if progress.exists() else ''
    frames=re.findall(r'^frame=(\d+)\s*$',events,re.M)
    times=re.findall(r'^out_time_us=(\d+)\s*$',events,re.M)
    count=int(frames[-1]) if frames else 0
    duration=int(times[-1])/1_000_000 if times else 0
    video_line=next((line for line in log.splitlines() if 'Stream #' in line and 'Video:' in line),'')
    size_match=re.search(r'\b(\d{2,5})x(\d{2,5})\b',video_line)
    fps_match=re.search(r'\b([\d.]+) fps\b',video_line)
    size=[int(size_match[1]),int(size_match[2])] if size_match else None
    fps=float(fps_match[1]) if fps_match else None
    audio=any('Stream #' in line and 'Audio:' in line for line in log.splitlines())
    peaks=re.findall(r'max_volume:\s*([\-\d.]+) dB',log)
    peak=float(peaks[-1]) if peaks else None
    failures=[]
    if process.returncode:
        failures.append(f'FFmpeg decode failed with exit code {process.returncode}.')
    if not count or 'progress=end' not in events:
        failures.append('No completed decoded video stream was found.')
    if expected_duration is not None and abs(duration-expected_duration)>max(.10,2/(fps or 30)):
        failures.append(f'Duration {duration:g}s does not match {expected_duration:g}s.')
    if expected_fps is not None and (fps is None or abs(fps-expected_fps)>.01):
        failures.append(f'Frame rate {fps} does not match {expected_fps:g}.')
    if expected_size is not None and size!=list(expected_size):
        failures.append(f'Resolution {size} does not match {list(expected_size)}.')
    if expected_duration is not None and expected_fps is not None:
        if abs(count-round(expected_duration*expected_fps))>1:
            failures.append('Decoded frame count does not match requested duration and frame rate.')
    if require_audio and not audio:
        failures.append('An audio stream was requested but not found.')
    if audio and peak is None:
        failures.append('Audio was present but its peak could not be measured.')
    if peak is not None and peak>=0:
        failures.append('Audio reaches 0 dBFS; inspect for clipping.')
    digest=hashlib.sha256()
    with video.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    result={'video':str(video),'sha256':digest.hexdigest(),'decode_passed':not failures,
            'decoded_frames':count,'decoded_duration_seconds':duration,'fps':fps,
            'resolution':size,'audio_present':audio,'audio_peak_dbfs':peak,'failures':failures,
            'visual_review_required':True}
    return result,log


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video',required=True)
    parser.add_argument('--ffmpeg')
    parser.add_argument('--expected-duration',type=float)
    parser.add_argument('--expected-fps',type=float)
    parser.add_argument('--expected-size',help='Example: 1920x1080')
    parser.add_argument('--require-audio',action='store_true')
    parser.add_argument('--output',help='Validation JSON; defaults beside the video.')
    args=parser.parse_args()
    try:
        size=tuple(map(int,args.expected_size.lower().split('x'))) if args.expected_size else None
        if size and len(size)!=2:
            raise ValueError('--expected-size must be WIDTHxHEIGHT')
        result,log=verify(args.video,args.ffmpeg,args.expected_duration,args.expected_fps,
                          size,args.require_audio)
    except (OSError,ValueError,RuntimeError) as exc:
        parser.exit(2,f'{exc}\n')
    output=Path(args.output) if args.output else Path(args.video).with_suffix('.validation.json')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    output.with_suffix('.log').write_text(log,encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))
    return 0 if result['decode_passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
