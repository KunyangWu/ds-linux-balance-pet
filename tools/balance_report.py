#!/usr/bin/env python3
import argparse
import csv
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo

WIDTH = 1000
HEIGHT = 560
INK = (40 / 255, 46 / 255, 70 / 255)
MUTED = (130 / 255, 138 / 255, 160 / 255)
LINE = (77 / 255, 107 / 255, 254 / 255)
BAR = (255 / 255, 146 / 255, 74 / 255)
GRID = (226 / 255, 230 / 255, 240 / 255)
BACKGROUND = (250 / 255, 251 / 255, 253 / 255)


def font(size, bold=False):
    desc = Pango.FontDescription()
    desc.set_family("Noto Sans CJK SC")
    desc.set_absolute_size(float(size) * Pango.SCALE)
    desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
    return desc


def make_layout(cr, text, size, bold=False):
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(font(size, bold))
    layout.set_text(str(text), -1)
    return layout


def text(cr, value, size, color, x, y, bold=False):
    layout = make_layout(cr, value, size, bold)
    cr.set_source_rgba(*color)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    return layout.get_pixel_size()


def load_rows(path, days):
    rows = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                balance = float(row["balance"])
                stamp = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            except (KeyError, TypeError, ValueError):
                continue
            rows.append(
                {
                    "time": stamp,
                    "balance": balance,
                    "currency": row.get("currency") or "",
                    "source": row.get("source") or "",
                }
            )
    if not rows:
        return []
    counts = Counter((row["source"], row["currency"]) for row in rows)
    chosen = counts.most_common(1)[0][0]
    rows = [row for row in rows if (row["source"], row["currency"]) == chosen]
    rows.sort(key=lambda row: row["time"])
    if days > 0:
        cutoff = datetime.now().timestamp() - days * 86400
        rows = [row for row in rows if row["time"].timestamp() >= cutoff]
    return rows


def hourly(rows):
    buckets = {}
    for previous, current in zip(rows, rows[1:]):
        delta = previous["balance"] - current["balance"]
        key = previous["time"].strftime("%Y%m%d%H")
        label = previous["time"].strftime("%m-%d %H时")
        entry = buckets.setdefault(key, {"label": label, "spend": 0.0})
        entry["spend"] += delta
    return [buckets[key] for key in sorted(buckets)]


def draw(rows, output):
    first = rows[0]
    last = rows[-1]
    spent = round(first["balance"] - last["balance"], 2)
    hours = round((last["time"] - first["time"]).total_seconds() / 3600, 2)
    rate = round(spent / hours, 2) if hours > 0 else 0.0
    by_hour = hourly(rows)
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, WIDTH, HEIGHT)
    cr = cairo.Context(surface)
    cr.set_source_rgba(*BACKGROUND)
    cr.paint()
    text(cr, "余额消耗记录", 14, INK, 24, 16, True)
    subtitle = (
        f"{first['time']:%m-%d %H:%M} → {last['time']:%m-%d %H:%M}　"
        f"共消耗 ¥{spent:.2f}　平均 ¥{rate:.2f}/小时　当前 ¥{last['balance']:.2f}"
    )
    text(cr, subtitle, 9, MUTED, 26, 46)
    plot_x, plot_y, plot_w, plot_h = 70.0, 84.0, WIDTH - 110.0, 250.0
    maximum = max(row["balance"] for row in rows)
    minimum = min(row["balance"] for row in rows)
    top = math.ceil(maximum * 1.1)
    bottom = max(0, math.floor(minimum * 0.9))
    if top <= bottom:
        top = bottom + 1
    total_minutes = max(1.0, (last["time"] - first["time"]).total_seconds() / 60)
    cr.set_source_rgba(*GRID)
    cr.set_line_width(1)
    cr.rectangle(plot_x, plot_y, plot_w, plot_h)
    cr.stroke()
    for index in range(5):
        value = bottom + (top - bottom) * index / 4.0
        y = plot_y + plot_h - plot_h * (value - bottom) / (top - bottom)
        cr.move_to(plot_x, y)
        cr.line_to(plot_x + plot_w, y)
        cr.stroke()
        text(cr, f"¥{value:.2f}", 8, MUTED, 20, y - 8)
    points = []
    for row in rows:
        minutes = (row["time"] - first["time"]).total_seconds() / 60
        x = plot_x + plot_w * minutes / total_minutes
        y = plot_y + plot_h - plot_h * (row["balance"] - bottom) / (top - bottom)
        points.append((x, y))
    if len(points) > 1:
        cr.set_source_rgba(*LINE)
        cr.set_line_width(3)
        cr.move_to(*points[0])
        for point in points[1:]:
            cr.line_to(*point)
        cr.stroke()
    cr.set_source_rgba(*LINE)
    for x, y in points:
        cr.arc(x, y, 3, 0, 2 * math.pi)
        cr.fill()
    text(cr, first["time"].strftime("%H:%M"), 8, MUTED, plot_x, plot_y + plot_h + 6)
    text(cr, last["time"].strftime("%H:%M"), 8, MUTED, plot_x + plot_w - 34, plot_y + plot_h + 6)
    bar_top, bar_h, bar_x, bar_area_w = 400.0, 110.0, 70.0, WIDTH - 130.0
    text(cr, "逐小时消耗", 9, INK, 24, bar_top - 24)
    max_spend = max((item["spend"] for item in by_hour), default=0)
    if max_spend <= 0:
        max_spend = 1
    slot = bar_area_w / max(1, len(by_hour))
    bar_w = max(6, min(46, slot * 0.6))
    for index, item in enumerate(by_hour):
        height = bar_h * (item["spend"] / max_spend)
        x = bar_x + index * slot + (slot - bar_w) / 2
        cr.set_source_rgba(*BAR)
        cr.rectangle(x, bar_top + bar_h - height, bar_w, height)
        cr.fill()
        text(cr, item["label"], 8, MUTED, x - 6, bar_top + bar_h + 6)
        text(cr, f"¥{item['spend']:.2f}", 8, INK, x - 6, bar_top + bar_h - height - 16)
    output.parent.mkdir(parents=True, exist_ok=True)
    surface.write_to_png(str(output))
    return {
        "first": first,
        "last": last,
        "spent": spent,
        "hours": hours,
        "rate": rate,
        "by_hour": by_hour,
        "output": output,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=str(Path.home() / ".local/state/ds-linux-balance-pet/balance-log.csv"))
    parser.add_argument("--out-dir", default=str(Path.home() / ".local/state/ds-linux-balance-pet/reports"))
    parser.add_argument("--days", type=int, default=0)
    args = parser.parse_args()
    log_path = Path(args.log)
    if not log_path.is_file():
        print("没有找到余额日志：" + str(log_path))
        print("桌宠运行后会自动生成。")
        return 1
    rows = load_rows(log_path, args.days)
    if len(rows) < 2:
        print(f"记录太少（{len(rows)} 条），至少两条才能算消耗。")
        return 0
    output = Path(args.out_dir) / ("余额报告-" + datetime.now().strftime("%Y%m%d-%H%M") + ".png")
    result = draw(rows, output)
    first = result["first"]
    last = result["last"]
    print()
    print(
        f"记录区间：{first['time']:%Y-%m-%d %H:%M} → {last['time']:%Y-%m-%d %H:%M}"
        f"（{result['hours']} 小时 / {len(rows)} 条）"
    )
    print(f"余额：{first['balance']:.2f} → {last['balance']:.2f}，共消耗 ¥{result['spent']:.2f}")
    print(f"平均消耗：¥{result['rate']:.2f} / 小时")
    if result["rate"] > 0:
        print(f"按这个速度，剩余 ¥{last['balance']:.2f} 大约还能用 {last['balance'] / result['rate']:.1f} 小时")
    print()
    print("逐小时消耗：")
    for item in result["by_hour"]:
        print(f"  {item['label']}   ¥{item['spend']:6.2f}")
    print()
    print("图表：" + str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
