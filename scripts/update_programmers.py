"""Record daily public Programmers badge snapshots and render an SVG chart.

Only Python's standard library is required. The badge is synchronized by the
user's Chrome extension; this script never signs in to Programmers.
"""

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from io import StringIO
from pathlib import Path
import re
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


BADGE_URL = "https://api.programmers-badge.jh8459.com/badge/8d0c133e8ea9.svg"
ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "data" / "programmers-history.csv"
CHART = ROOT / "assets" / "programmers-growth.svg"
KST = timezone(timedelta(hours=9))
NS = {"s": "http://www.w3.org/2000/svg"}
FIELDS = ["date", "solved", "score", "rank", "skill_level", "observed_at_kst"]
MAX_BYTES = 512_000


@dataclass(frozen=True)
class Stats:
    solved: int
    score: int
    rank: int
    skill_level: int


def parse_number(value: str) -> int:
    value = value.strip()
    if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)", value):
        raise ValueError(f"Unexpected badge number: {value!r}")
    return int(value.replace(",", ""))


def parse_badge(content: bytes) -> Stats:
    if len(content) > MAX_BYTES or b"<!ENTITY" in content.upper():
        raise ValueError("Badge is too large or contains unsupported XML entities")
    root = ET.fromstring(content)
    if root.tag != f"{{{NS['s']}}}svg":
        raise ValueError("The response is not an SVG badge")
    metrics = {}
    for group in root.findall(".//s:g", NS):
        labels = [n for n in group.findall("s:text", NS)
                  if "sub_title" in n.get("class", "").split()]
        values = [n for n in group.findall("s:text", NS)
                  if "value" in n.get("class", "").split()]
        if len(labels) != 1 or len(values) != 1:
            continue
        label = "".join(labels[0].itertext()).strip()
        if label in {"Solved", "Score", "Rank"}:
            if label in metrics:
                raise ValueError(f"Duplicate metric: {label}")
            metrics[label] = parse_number("".join(values[0].itertext()))
    levels = [n for n in root.findall(".//s:text", NS)
              if "title_no" in n.get("class", "").split()]
    if set(metrics) != {"Solved", "Score", "Rank"} or len(levels) != 1:
        raise ValueError("Badge format changed: required statistics were not found")
    level = parse_number("".join(levels[0].itertext()))
    if not 0 <= level <= 5:
        raise ValueError("Invalid skill level")
    return Stats(metrics["Solved"], metrics["Score"], metrics["Rank"], level)


def fetch_badge() -> bytes:
    request = Request(BADGE_URL, headers={
        "User-Agent": "y0y00700-programmers-growth/1.0",
        "Accept": "image/svg+xml",
        "Cache-Control": "no-cache",
    })
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                return response.read(MAX_BYTES + 1)
        except (URLError, TimeoutError, OSError):
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError("Badge download failed")


def load_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != FIELDS:
            raise ValueError("Unexpected history columns; keeping existing files")
        rows = list(reader)
    seen = set()
    for row in rows:
        date.fromisoformat(row["date"])
        if row["date"] in seen:
            raise ValueError("History contains duplicate dates")
        seen.add(row["date"])
        for field in ("solved", "score", "rank", "skill_level"):
            if not row[field].isascii() or not row[field].isdigit():
                raise ValueError(f"Invalid history value for {field}")
        if int(row["skill_level"]) > 5:
            raise ValueError("Invalid skill level in history")
        stamp = datetime.fromisoformat(row["observed_at_kst"])
        if stamp.utcoffset() != timedelta(hours=9):
            raise ValueError("History timestamps must use Korean time")
    return sorted(rows, key=lambda row: row["date"])


def upsert_snapshot(rows: list[dict], stats: Stats, now: datetime) -> list[dict]:
    today = now.astimezone(KST).date().isoformat()
    if rows and rows[-1]["date"] > today:
        raise ValueError("Refusing to insert a snapshot before existing history")
    row = {"date": today, "solved": str(stats.solved), "score": str(stats.score),
           "rank": str(stats.rank), "skill_level": str(stats.skill_level),
           "observed_at_kst": now.astimezone(KST).isoformat(timespec="seconds")}
    existing = next((old for old in rows if old["date"] == today), None)
    # Re-running without changes on the same day creates no needless commit.
    if existing and all(existing[field] == row[field] for field in FIELDS[:-1]):
        row = existing
    return sorted([old for old in rows if old["date"] != today] + [row],
                  key=lambda item: item["date"])


def history_csv(rows: list[dict]) -> str:
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def render_chart(rows: list[dict]) -> str:
    if not rows:
        raise ValueError("A chart requires at least one verified snapshot")
    latest, first = rows[-1], rows[0]
    start = date.fromisoformat(first["date"]).toordinal()
    end = date.fromisoformat(latest["date"]).toordinal()
    span = max(end - start, 1)
    top = max(5, ((max(int(r["solved"]) for r in rows) + 4) // 5) * 5)
    left, right, upper, lower = 64, 680, 140, 280
    height = lower - upper
    def point(row):
        x = left + (date.fromisoformat(row["date"]).toordinal() - start) / span * (right - left)
        y = lower - int(row["solved"]) / top * height
        return x, y
    points = [point(row) for row in rows]
    line = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    change = int(latest["solved"]) - int(first["solved"])
    change_text = f"{change:+,}"
    subtitle = (f"누적 {int(latest['solved']):,}문제 · 기록 시작 이후 {change_text}문제"
                f" · {len(rows):,}일 기록")
    stamp = latest["observed_at_kst"].replace("T", " ")[:16]
    footer = f"마지막 수집 {stamp} KST · 공개 배지에서 수집한 값"
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="370" viewBox="0 0 720 370" role="img" aria-labelledby="title desc">',
        '<title id="title">프로그래머스 누적 문제 풀이 성장 기록</title>',
        f'<desc id="desc">{escape(subtitle)}. {escape(first["date"])}부터 {escape(latest["date"])}까지 수집한 통계입니다. 날짜 사이 선은 관측값을 연결하며 미수집일의 풀이를 뜻하지 않습니다.</desc>',
        '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Malgun Gothic",sans-serif;fill:#334155}.muted{fill:#64748b;font-size:12px}.tick{fill:#64748b;font-size:11px}</style>',
        '<rect x=".5" y=".5" width="719" height="369" rx="16" fill="#fff" stroke="#e2e8f0"/>',
        '<text x="32" y="38" font-size="20" font-weight="700">프로그래머스 성장 기록</text>',
        f'<text x="32" y="65" font-size="14">{escape(subtitle)}</text>',
        f'<text x="32" y="91" class="muted">Score {int(latest["score"]):,} · Skill {latest["skill_level"]} · Rank {int(latest["rank"]):,}</text>',
        '<text x="32" y="120" class="muted">누적 풀이 수</text>',
    ]
    for i in range(6):
        value = top * i // 5
        y = lower - i / 5 * height
        parts.append(f'<line x1="{left}" y1="{y:.2f}" x2="{right}" y2="{y:.2f}" stroke="#e2e8f0"/>')
        parts.append(f'<text x="{left-12}" y="{y+4:.2f}" text-anchor="end" class="tick">{value:,}</text>')
    if len(points) > 1:
        parts.append(f'<polyline points="{line}" fill="none" stroke="#2563eb" stroke-width="2.5" stroke-linejoin="round"/>')
    for row, (x, y) in zip(rows, points):
        parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="#2563eb"><title>{row["date"]}: {int(row["solved"]):,}문제</title></circle>')
    if len(rows) == 1:
        parts.append('<text x="372" y="210" text-anchor="middle" class="muted">첫 기록입니다. 다음 기록부터 변화가 표시됩니다.</text>')
    parts.append(f'<text x="{left}" y="306" class="tick">{first["date"]}</text>')
    if end > start:
        parts.append(f'<text x="{right}" y="306" text-anchor="end" class="tick">{latest["date"]}</text>')
    parts.append(f'<text x="32" y="346" class="muted">{escape(footer)}</text>')
    parts.append("</svg>\n")
    return "\n".join(parts)


def write_if_changed(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def main() -> None:
    stats = parse_badge(fetch_badge())
    rows = upsert_snapshot(load_history(HISTORY), stats, datetime.now(KST))
    # Validate and render everything before touching either output file.
    csv_content, chart_content = history_csv(rows), render_chart(rows)
    write_if_changed(HISTORY, csv_content)
    write_if_changed(CHART, chart_content)
    print(f"Snapshot saved: {rows[-1]['date']} KST; solved={stats.solved}; score={stats.score}; skill={stats.skill_level}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, ET.ParseError, URLError, TimeoutError, OSError) as error:
        print(f"Programmers update failed; no fabricated snapshot was added: {error}", file=sys.stderr)
        sys.exit(1)
