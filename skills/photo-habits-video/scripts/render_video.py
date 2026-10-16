#!/usr/bin/env python3
"""Render a local photography-statistics animation; never calls an image/video API.

Dependencies: Pillow, NumPy, FFmpeg with libx264; a Chinese-capable font.
Charts accept an empty distribution and show missing-data messages. Optional
assets JSON: {"camera": {"EXIF model": "sprite.png"}, "lens": {...}}. Paths
are resolved relative to that JSON. Only use images you are authorized to use.
"""
import argparse
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
CYAN, WHITE, MUTED = (64, 218, 244), (237, 245, 251), (145, 165, 187)
DIM, LINE, PANEL, BLUE = (74, 96, 124), (31, 49, 72), (13, 24, 40), (64, 124, 178)
AMBER = (255, 193, 95)
CHAPTERS = [('intro', '序章', 4), ('focal_length', '焦段', 6), ('aperture', '光圈', 6),
            ('iso', 'ISO', 6), ('shutter', '快门', 6), ('hours', '时段', 7),
            ('monthly', '月份', 12), ('camera', '机型', 6), ('lens', '镜头', 7)]


def clamp(value, low=0, high=1):
    return max(low, min(high, value))


def ease(value):
    return 1 - (1 - clamp(value)) ** 3


def mix(a, b, value):
    return tuple(round(x + (y - x) * clamp(value)) for x, y in zip(a, b))


def number(value, fallback=0):
    try:
        result = float(value)
        return result if math.isfinite(result) else fallback
    except (TypeError, ValueError):
        return fallback


def find_font(requested=None):
    if requested:
        path = Path(requested).expanduser()
        if path.is_file():
            return path
        raise ValueError(f'Font file does not exist: {path}')
    candidates = [os.environ.get('PHOTO_VIDEO_FONT', ''),
                  str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/msyh.ttc'),
                  str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/simhei.ttf'),
                  '/System/Library/Fonts/PingFang.ttc',
                  '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                  '/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf',
                  '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc']
    for name in candidates:
        if name and Path(name).is_file():
            return Path(name)
    raise ValueError('No Chinese-capable font found. Install Noto Sans CJK or pass --font /path/to/font.ttf. Fonts are not bundled.')


def find_ffmpeg(requested=None):
    for candidate in [requested, os.environ.get('FFMPEG_BINARY'), shutil.which('ffmpeg')]:
        if candidate:
            resolved = shutil.which(str(candidate)) or (str(Path(candidate).resolve()) if Path(candidate).is_file() else None)
            if resolved:
                return resolved
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise ValueError('FFmpeg not found. Install FFmpeg or imageio-ffmpeg, or pass --ffmpeg PATH.') from None


def row_label(row, key):
    label = row.get('display_name') or row.get('display') or row.get('label')
    if label:
        return str(label)
    value = row.get('value', '未知')
    if key == 'focal_length':
        return f'{number(value):g} mm'
    if key == 'aperture':
        return f'f/{number(value):g}'
    if key == 'iso':
        return f'ISO {number(value):g}'
    if key == 'shutter_top':
        seconds = number(value)
        return (f'1/{round(1 / seconds):g} s' if 0 < seconds < 1 else f'{seconds:g} s')
    return str(value)


class Renderer:
    def __init__(self, data, font_path, duration, size=(W, H), assets=None):
        self.data, self.font_path, self.duration, self.size = data, str(font_path), duration, size
        self.synthetic = bool(data.get('synthetic'))
        self.assets = assets or {}
        self.scenes, cursor = [], 0
        for key, label, weight in CHAPTERS:
            end = cursor + weight / 60 * duration
            self.scenes.append((cursor, end, key, label))
            cursor = end
        dr = data.get('date_range') or data.get('date_range_detail') or []
        if isinstance(dr, dict):
            dr = [dr.get('start'), dr.get('end')]
        self.date_label = ' — '.join(str(v)[:10] for v in dr if v) or '日期记录不足'
        self.notes = [str(v) for v in (data.get('coverage_notes_zh') or data.get('coverage_notes') or []) if v]
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        glow = np.exp(-(((xx - 1500) / 750) ** 2 + ((yy - 180) / 700) ** 2))
        arr = np.empty((H, W, 3), dtype=np.uint8)
        for channel, (floor, strength) in enumerate(((6, 5), (12, 16), (22, 26))):
            arr[:, :, channel] = floor + glow * strength
        self.background = Image.fromarray(arr)
        self.captions = self.make_captions()

    @lru_cache(maxsize=100)
    def font(self, size):
        return ImageFont.truetype(self.font_path, round(size))

    def text(self, draw, xy, value, size=32, fill=WHITE, anchor=None, max_width=None):
        value = str(value)
        if max_width:
            while size > 17 and draw.textlength(value, font=self.font(size)) > max_width:
                size -= 1
            if draw.textlength(value, font=self.font(size)) > max_width:
                while value and draw.textlength(value + '…', font=self.font(size)) > max_width:
                    value = value[:-1]
                value += '…'
        draw.text(xy, value, font=self.font(size), fill=fill, anchor=anchor)

    @staticmethod
    def box(draw, box, fill=PANEL, outline=None, width=1, radius=22):
        draw.rounded_rectangle(tuple(round(x) for x in box), radius, fill, outline, width)

    def rows(self, key):
        value = self.data.get(key) or []
        return [r for r in value if isinstance(r, dict) and number(r.get('count')) > 0] if isinstance(value, list) else []

    def make_captions(self):
        caps = {'intro': '从光线到时间，用拍摄记录观察自己的摄影习惯。',
                'hours': '按照片所记当地时间统计；未记录时区的照片不推断时区。',
                'monthly': '每年分别归一显示颜色；数量为现有素材记录，不代表完整全年。',
                'shutter': '曝光越长，同速移动光点的轨迹越长；动画是原理示意。',
                'aperture': '同一焦距下，光圈直径与 f 值成反比；动画是原理示意。',
                'lens': '这些数字，记录了你选择光线与时间的方式。'}
        for key in ('focal_length', 'iso', 'camera', 'lens'):
            rows = self.rows(key)
            if rows:
                caps[key] = f'{row_label(rows[0], key)} 出现最多，占有该参数记录的 {number(rows[0].get("percent")):.1f}%。'
            else:
                caps[key] = '当前素材缺少这一参数，保留空缺，不推测拍摄设置。'
        return caps

    def base(self, index, t, title, subtitle):
        im = self.background.copy()
        d = ImageDraw.Draw(im)
        self.text(d, (125, 46), '我的摄影习惯', 27, MUTED)
        self.text(d, (1795, 46), self.date_label, 25, MUTED, 'ra')
        d.line((125, 103, 1795, 103), fill=LINE, width=2)
        if self.synthetic:
            self.box(d, (830, 37, 1090, 86), fill=(75, 43, 20), outline=AMBER, radius=12)
            self.text(d, (960, 61), '演示数据', 29, AMBER, 'mm')
        self.text(d, (130, 134), f'{index:02d} / {self.scenes[index][3]}', 25, CYAN)
        self.text(d, (130, 188), title, 57, WHITE, max_width=1660)
        self.text(d, (133, 268), subtitle, 28, MUTED, max_width=1660)
        for i, (start, end, _, label) in enumerate(self.scenes[1:]):
            x = 134 + i * 208
            self.text(d, (x, 997), label, 22, CYAN if i + 1 == index else MUTED)
            d.line((x, 1036, x + 168, 1036), fill=LINE, width=3)
            progress = clamp((t - start) / max(end - start, .001))
            if progress:
                d.line((x, 1036, x + 168 * progress, 1036), fill=CYAN, width=3)
        cap = self.captions.get(self.scenes[index][2], '')
        self.box(d, (150, 919, 1770, 979), fill=(13, 25, 40), radius=17)
        self.text(d, (960, 948), cap, 27, WHITE, 'mm', max_width=1550)
        return im, d

    def no_data(self, d, metric):
        self.box(d, (285, 405, 1635, 778), outline=LINE)
        self.text(d, (960, 535), '暂无可用的' + metric + '记录', 52, MUTED, 'mm')
        self.text(d, (960, 633), '缺失参数保留空缺，其他维度仍可继续统计。', 30, MUTED, 'mm')

    def bars(self, d, rows, key, local, x=850, y=405, width=930, gap=87, limit=5):
        rows = rows[:limit]
        maximum = max([number(r.get('percent')) for r in rows] + [1])
        for i, row in enumerate(rows):
            top = y + i * gap
            p = ease(local / 1.25 - i * .07)
            label = row_label(row, key)
            self.text(d, (x, top), label, 29, WHITE, max_width=width - 170)
            self.text(d, (x + width, top), f'{number(row.get("percent")) * p:.1f}%', 30, CYAN if i == 0 else WHITE, 'ra')
            self.box(d, (x, top + 43, x + width - 170, top + 62), fill=(21, 38, 58), radius=7)
            bar = (width - 170) * number(row.get('percent')) / maximum * p
            if bar > 1:
                self.box(d, (x, top + 43, x + bar, top + 62), fill=CYAN if i == 0 else BLUE, radius=7)
            self.text(d, (x + width, top + 43), f'{round(number(row.get("count")) * p):,} 张', 20, MUTED, 'ra')

    def intro(self, local, t, index):
        im, d = self.base(index, t, '我的摄影习惯', '每一次按下快门，都留下了选择。')
        p = ease(local / 1.25)
        count = int(number(self.data.get('valid_capture_records', self.data.get('valid_camera_records'))))
        self.text(d, (130, 427), f'{round(count * p):,}', 142, CYAN)
        self.text(d, (137, 610), '条有效拍摄记录', 38)
        self.text(d, (137, 718), f'{int(number(self.data.get("shooting_days"))):,} 个拍摄日期 · 八个观察维度', 30, MUTED)
        for radius in [130, 205, 275]:
            r = radius * (.8 + .2 * p)
            d.ellipse((1500 - r, 590 - r, 1500 + r, 590 + r), outline=mix(LINE, CYAN, .25), width=3)
        self.text(d, (1500, 590), '光 · 时间', 45, WHITE, 'mm')
        if self.notes:
            self.text(d, (137, 839), self.notes[0], 24, MUTED, max_width=1650)
        return im

    def distribution(self, local, t, index, key):
        titles = {'focal_length': ('你最常用的焦段', '按实际焦距统计；不同画幅不自动等效换算'),
                  'iso': ('感光度，记录光线的强弱', '排名来自有效 ISO 记录'),
                  'camera': ('陪你按下快门的机型', '按有机型记录的照片计算占比'),
                  'lens': ('你最常用的镜头', '缺失或无法识别的型号保持未知')}
        im, d = self.base(index, t, *titles[key])
        rows = self.rows(key)
        if not rows:
            self.no_data(d, titles[key][0][-2:])
            return im
        row = rows[0]
        self.box(d, (130, 352, 775, 865), outline=LINE)
        self.text(d, (170, 390), '最常出现', 28, MUTED)
        asset = self.asset(key, row.get('value'))
        if asset:
            p = ease(local / .8)
            x, y = 452 - asset.width // 2, round(577 - asset.height // 2 + 30 * (1 - p))
            im.paste(asset, (x, y), asset)
            self.text(d, (452, 746), row_label(row, key), 39, WHITE, 'mm', max_width=570)
            self.text(d, (452, 806), f'{number(row.get("percent")) * ease(local):.1f}%', 44, CYAN, 'mm')
        else:
            self.text(d, (170, 506), row_label(row, key), 83, CYAN, max_width=565)
            self.text(d, (170, 638), f'{number(row.get("percent")) * ease(local):.1f}%', 68)
            self.text(d, (174, 748), f'{int(number(row.get("count"))):,} 张照片', 31, MUTED)
        self.bars(d, rows, key, local)
        if key == 'focal_length':
            values = [number(r.get('value')) for r in rows if number(r.get('value')) > 0]
            if values:
                lo, hi = min(values), max(values)
                for i in range(8):
                    x = 175 + i * 72
                    d.line((x, 820, x, 837), fill=DIM, width=2)
                x = 175 + (math.sin(local * 1.3) + 1) * 252
                d.ellipse((x - 5, 823, x + 5, 833), fill=CYAN)
                self.text(d, (175, 780), f'{lo:g} — {hi:g} mm · 动态标尺示意', 21, MUTED)
        return im

    @lru_cache(maxsize=32)
    def asset(self, key, value):
        source = self.assets.get(key, {}).get(str(value))
        if not source:
            return None
        image = Image.open(source).convert('RGBA')
        bbox = image.getchannel('A').getbbox()
        if bbox:
            image = image.crop(bbox)
        image.thumbnail((540, 260), Image.Resampling.LANCZOS)
        return image

    def aperture(self, local, t, index):
        im, d = self.base(index, t, '光圈，怎样控制开口', '相同焦距下，小 f 值对应大开口；统计与示意分开呈现')
        rows = self.rows('aperture')
        if not rows:
            self.no_data(d, '光圈')
            return im
        values = sorted({number(r.get('value')) for r in rows if number(r.get('value')) > 0})
        if not values:
            self.no_data(d, '光圈')
            return im
        # Values are drawn from this dataset; diameter is exactly proportional to 1/f.
        fmin, fmax = min(values), max(values)
        fvalue = math.exp(math.log(fmin) + (math.log(fmax) - math.log(fmin)) * (.5 - .5 * math.cos(local * 1.5)))
        cx, cy, outer = 445, 607, 235
        d.ellipse((cx - outer - 13, cy - outer - 13, cx + outer + 13, cy + outer + 13), fill=PANEL, outline=LINE, width=4)
        opening = 198 * fmin / fvalue
        rotation = local * .03
        hole = [(cx + opening * math.cos(rotation + j * 2 * math.pi / 9), cy + opening * math.sin(rotation + j * 2 * math.pi / 9)) for j in range(9)]
        for j in range(9):
            a1 = rotation + j * 2 * math.pi / 9
            a2 = rotation + (j + 1) * 2 * math.pi / 9
            polygon = [hole[j], hole[(j + 1) % 9],
                       (cx + outer * math.cos(a2 + .4), cy + outer * math.sin(a2 + .4)),
                       (cx + outer * math.cos(a1 + .4), cy + outer * math.sin(a1 + .4))]
            d.polygon(polygon, fill=mix((26, 47, 67), (46, 79, 96), j / 9), outline=LINE)
        d.polygon(hole, fill=(3, 9, 16), outline=CYAN, width=3)
        self.text(d, (445, 836), f'f/{fvalue:.1f} · 开口示意', 30, CYAN, 'mm')
        self.bars(d, rows, 'aperture', local)
        return im

    def shutter(self, local, t, index):
        im, d = self.base(index, t, '快门，怎样留住运动', '左侧为同速运动示意；右侧为真实快门统计')
        rows, bins = self.rows('shutter_top'), self.rows('shutter_bins')
        if not rows and not bins:
            self.no_data(d, '快门')
            return im
        self.box(d, (130, 351, 776, 865), outline=LINE)
        # Pedagogical examples, explicitly labeled, are never included in statistics.
        demos = [.001, .01, .25, 1.0]
        exposure = demos[int(local / 1.35) % len(demos)]
        self.text(d, (170, 391), '曝光时长示意', 28, MUTED)
        self.text(d, (170, 465), row_label({'value': exposure}, 'shutter_top'), 83, CYAN)
        left, width, y = 175, 555, 681
        position = lambda at: left + 35 + (width - 70) * (.5 + .5 * math.sin(at * 1.7))
        d.line((left, y, left + width, y), fill=LINE, width=2)
        for j in range(80):
            x1 = position(local - exposure * (1 - j / 80))
            x2 = position(local - exposure * (1 - (j + 1) / 80))
            d.line((x1, y, x2, y), fill=mix(PANEL, CYAN, (j + 1) / 80), width=13)
        x = position(local)
        d.ellipse((x - 9, y - 9, x + 9, y + 9), fill=WHITE)
        self.text(d, (170, 784), '原理示意，不代表拍摄次数', 24, MUTED)
        if bins:
            self.bars(d, bins, 'shutter_bins', local)
        else:
            self.bars(d, rows, 'shutter_top', local)
        return im

    def hours(self, local, t, index):
        im, d = self.base(index, t, '你喜欢在几点按下快门', '太阳与月亮展示时间变化；柱高为该小时的照片数')
        rows = self.rows('hours_chronological')
        counts = [0] * 24
        for row in rows:
            try:
                hour = int(str(row.get('value')).split(':')[0])
                if 0 <= hour < 24:
                    counts[hour] += int(number(row.get('count')))
            except (ValueError, TypeError):
                pass
        if not sum(counts):
            self.no_data(d, '拍摄时间')
            return im
        hour = (local / 6.5 * 24) % 24
        daylight = max(0, math.sin((hour - 6) / 12 * math.pi)) if 6 <= hour <= 18 else 0
        sky = mix((10, 22, 44), (33, 102, 139), daylight)
        self.box(d, (130, 351, 1775, 560), fill=sky, outline=LINE)
        sun = 6 <= hour < 18
        cycle = (hour - 6) / 12 if sun else ((hour - 18) % 24) / 12
        x, y = 190 + 1500 * cycle, 500 - 100 * math.sin(cycle * math.pi)
        color = (255, 213, 129) if sun else (217, 235, 249)
        d.ellipse((x - 29, y - 29, x + 29, y + 29), fill=color)
        if not sun:
            d.ellipse((x - 11, y - 37, x + 40, y + 17), fill=sky)
            for sx, sy in [(300, 399), (610, 462), (1060, 391), (1450, 455)]:
                d.ellipse((sx - 2, sy - 2, sx + 2, sy + 2), fill=WHITE)
        self.text(d, (1715, 378), f'{int(hour):02d}:00', 44, WHITE, 'ra')
        maximum = max(counts)
        for i, count in enumerate(counts):
            x = 143 + i * 68
            bar = 226 * count / maximum * ease(local / 1.3)
            if bar > 0:
                self.box(d, (x, 830 - bar, x + 45, 830), fill=CYAN if count == maximum else BLUE, radius=7)
            self.text(d, (x + 22, 858), str(i), 23, MUTED, 'mm')
        peak = counts.index(maximum)
        self.text(d, (133, 577), f'单小时高峰 {peak:02d}:00 · {maximum:,} 张', 28, CYAN)
        return im

    def monthly(self, local, t, index):
        im, d = self.base(index, t, '每一年，哪些月份拍得最多', '每年独立归一颜色；单元格数字为该月照片数')
        rows = self.data.get('annual_monthly') or []
        rows = [r for r in rows if isinstance(r, dict)]
        if not rows:
            self.no_data(d, '月份')
            return im
        # Large archives cycle through eight-year pages rather than hiding years.
        pages = math.ceil(len(rows) / 8)
        page = min(pages - 1, int(local / 12 * pages))
        rows = rows[page * 8:(page + 1) * 8]
        if pages > 1:
            self.text(d, (1780, 322), f'{page + 1}/{pages} 页', 23, MUTED, 'ra')
        y0, gap, x0, step = 414, 56, 296, 106
        for month in range(12):
            self.text(d, (x0 + month * step + 45, 380), f'{month + 1}月', 22, MUTED, 'mm')
        self.text(d, (1780, 369), '年合计', 23, MUTED, 'ra')
        active = int(local * .65) % len(rows)
        for i, row in enumerate(rows):
            y = y0 + i * gap
            months = list(row.get('months', row.get('monthly_photo_counts', [])))[:12]
            months += [0] * (12 - len(months))
            values = [max(0, int(number(v))) for v in months]
            maximum = max(values) or 1
            year = row.get('year', '未知')
            self.text(d, (133, y + 8), year, 28, CYAN if i == active else WHITE)
            for j, count in enumerate(values):
                x = x0 + j * step
                fill = mix((14, 27, 43), (39, 143, 170), count / maximum * ease(local / 1.2))
                self.box(d, (x, y, x + 94, y + 46), fill=fill,
                         outline=CYAN if i == active and count == maximum and count else None,
                         width=2, radius=8)
                self.text(d, (x + 47, y + 23), f'{count:,}' if count else '—', 23, WHITE if count else DIM, 'mm')
            total = int(number(row.get('count', row.get('photo_count', sum(values)))))
            self.text(d, (1780, y + 7), f'{total:,}', 27, WHITE, 'ra')
        note = self.notes[0] if self.notes else '仅反映现有素材；起止年份、缺失归档年份可能覆盖不完整。'
        self.text(d, (134, 875), note, 23, MUTED, max_width=1645)
        return im

    def frame(self, t):
        index = next((i for i, (a, b, _, _) in enumerate(self.scenes) if a <= t < b), len(self.scenes) - 1)
        start, end, key, _ = self.scenes[index]
        # Normalize chapter animation time so short previews keep the same motion.
        nominal = CHAPTERS[index][2]
        local = clamp((t - start) / max(end - start, 1e-6)) * nominal
        if key in ('focal_length', 'iso', 'camera', 'lens'):
            im = self.distribution(local, t, index, key)
        else:
            im = getattr(self, key)(local, t, index)
        if self.size != (W, H):
            im = im.resize(self.size, Image.Resampling.LANCZOS)
        return im


def load_assets(path):
    if not path:
        return {}
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    result = {}
    for key in ('camera', 'lens'):
        mapping = value.get(key, {})
        if not isinstance(mapping, dict):
            raise ValueError(f'assets.{key} must be a mapping of EXIF value to image path')
        result[key] = {}
        for name, asset in mapping.items():
            image_path = Path(asset)
            if not image_path.is_absolute():
                image_path = path.parent / image_path
            if not image_path.is_file():
                raise ValueError(f'Asset does not exist: {image_path}')
            result[key][str(name)] = str(image_path.resolve())
    return result


def srt_time(seconds):
    milliseconds = round(seconds * 1000)
    hours, rem = divmod(milliseconds, 3600000)
    minutes, rem = divmod(rem, 60000)
    secs, ms = divmod(rem, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}'


def write_subtitles(renderer, path):
    blocks = []
    for i, (start, end, key, _) in enumerate(renderer.scenes, 1):
        cap = renderer.captions.get(key, '')
        prefix = '【演示数据】' if renderer.synthetic else ''
        blocks.append(f'{i}\n{srt_time(start)} --> {srt_time(end)}\n{prefix}{cap}\n')
    path.write_text('\n'.join(blocks), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--summary', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--duration', type=float, default=60, help='1–120 seconds; chapter lengths scale proportionally')
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--size', default='1920x1080', help='16:9 even dimensions, e.g. 960x540')
    parser.add_argument('--preview', action='store_true', help='Only write sample frames/contact sheet; no music or video')
    parser.add_argument('--preview-dir', type=Path)
    parser.add_argument('--font', type=Path)
    parser.add_argument('--ffmpeg')
    parser.add_argument('--assets', type=Path)
    audio = parser.add_mutually_exclusive_group()
    audio.add_argument('--music', type=Path, help='Use a supplied WAV instead of locally synthesized music')
    audio.add_argument('--silent', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.duration <= 120 or not 1 <= args.fps <= 60:
        parser.error('duration must be 1–120 and fps must be 1–60')
    try:
        width, height = (int(v) for v in args.size.lower().split('x'))
        if width < 320 or height < 180 or width % 2 or height % 2 or width * 9 != height * 16:
            raise ValueError('size must be 16:9, even dimensions, at least 320x180')
        data_bytes = args.summary.read_bytes()
        data = json.loads(data_bytes.decode('utf-8-sig'))
        if not isinstance(data, dict):
            raise ValueError('summary must be a JSON object')
        renderer = Renderer(data, find_font(args.font), args.duration, (width, height), load_assets(args.assets))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        preview_dir = args.preview_dir or args.output.parent / (args.output.stem + '_previews')
        preview_dir.mkdir(parents=True, exist_ok=True)
        samples = []
        for i, (start, end, key, _) in enumerate(renderer.scenes):
            stamp = start + (end - start) * .62
            frame = renderer.frame(stamp)
            destination = preview_dir / f'{i:02d}_{key}.png'
            frame.save(destination)
            thumb = frame.copy()
            thumb.thumbnail((640, 360), Image.Resampling.LANCZOS)
            samples.append(thumb)
        sheet = Image.new('RGB', (640 * 3, 360 * 3), (6, 12, 22))
        for i, sample in enumerate(samples):
            sheet.paste(sample, ((i % 3) * 640, (i // 3) * 360))
        sheet.save(preview_dir / 'contact_sheet.jpg', quality=92)
        srt_path = args.output.with_suffix('.srt')
        write_subtitles(renderer, srt_path)
        frames = round(args.duration * args.fps)
        actual_duration = frames / args.fps
        manifest = {'schema_version': 1, 'mode': 'preview' if args.preview else 'render',
                    'summary_sha256': hashlib.sha256(data_bytes).hexdigest(),
                    'synthetic': renderer.synthetic, 'requested_duration_seconds': args.duration,
                    'duration_seconds': actual_duration, 'fps': args.fps, 'frame_count': frames,
                    'width': width, 'height': height, 'font': str(renderer.font_path),
                    'subtitle_file': str(srt_path.resolve()), 'preview_directory': str(preview_dir.resolve()),
                    'encoded': False, 'full_decode_verified': False, 'audio': None,
                    'chapter_order': [r[2] for r in renderer.scenes], 'coverage_notes': renderer.notes}
        if not args.preview:
            ffmpeg = find_ffmpeg(args.ffmpeg)
            audio_path = None
            if args.music:
                if not args.music.is_file():
                    raise ValueError(f'Music file does not exist: {args.music}')
                audio_path = args.music
                manifest['audio'] = {'source': 'user_supplied', 'path': str(audio_path.resolve())}
            elif not args.silent:
                from make_music import generate_music
                audio_path = args.output.with_suffix('.music.wav')
                manifest['audio'] = {'source': 'original_local_synthesis', **generate_music(audio_path, actual_duration)}
            command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo',
                       '-vcodec', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{width}x{height}',
                       '-r', str(args.fps), '-i', 'pipe:0']
            if audio_path:
                command += ['-i', str(audio_path), '-map', '0:v:0', '-map', '1:a:0',
                            '-af', 'apad', '-c:a', 'aac', '-b:a', '192k']
            else:
                command += ['-an']
            command += ['-t', str(actual_duration), '-c:v', 'libx264', '-preset', 'fast',
                        '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(args.output)]
            # Log to a file to avoid a blocked stderr pipe on a verbose encoder failure.
            log_path = args.output.with_suffix('.encode.log')
            begin = time.monotonic()
            with log_path.open('wb') as log:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
                try:
                    for i in range(frames):
                        process.stdin.write(renderer.frame(i / args.fps).tobytes())
                    process.stdin.close()
                    code = process.wait()
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
            if code:
                raise RuntimeError(f'FFmpeg exited {code}; inspect {log_path}')
            manifest.update(encoded=True, encoder='FFmpeg/libx264', pixel_format='yuv420p',
                            encode_elapsed_seconds=round(time.monotonic() - begin, 2),
                            output_file=str(args.output.resolve()), output_bytes=args.output.stat().st_size,
                            output_sha256=hashlib.sha256(args.output.read_bytes()).hexdigest())
        manifest_path = args.output.with_suffix('.manifest.json')
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'manifest': str(manifest_path), 'preview': str(preview_dir / 'contact_sheet.jpg'),
                          'encoded': manifest['encoded'], 'full_decode_verified': False}, ensure_ascii=False))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        parser.exit(1, f'Render failed: {exc}\n')


if __name__ == '__main__':
    main()
