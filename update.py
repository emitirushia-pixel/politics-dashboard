#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政治ダッシュボード 夜間更新スクリプト

役割:
  - 公開情報源（e-Gov パブリックコメント公式RSS、衆議院の法案一覧）を取得する
  - GEMINI_API_KEY がある場合だけ、Gemini でやさしい日本語に要約して data.json を更新する
  - キーが無い場合は警告を出して正常終了し、data.json には一切触らない
  - 検証に失敗したら絶対に上書きしない（良いデータを守る）
  - 更新できなかったテーマの項目は前回のものをそのまま残す

依存: Python 標準ライブラリ + requests のみ
"""
import copy
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

JST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "data.json")
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
TOPICS = {"life", "diet", "deadline", "election", "poll"}
TIMEOUT = 30
HEADERS = {"User-Agent": "politics-dashboard-updater/1.0 (+nightly personal use)"}

EGOV_RSS = "https://public-comment.e-gov.go.jp/rss/pcm_list.xml"
DIET_URL_CANDIDATES = [
    # 衆議院「議案」一覧まわり。構成が変わったら fail soft で前回データを保持する。
    "https://www.shugiin.go.jp/internet/itdb_gian.nsf/html/gian/menu.htm",
    "https://www.shugiin.go.jp/internet/itdb_gian.nsf/html/gian/kaiji219.htm",
]


def log(msg):
    print(f"[update] {msg}", flush=True)


def fetch_text(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def find_dates(text):
    """テキスト中の日付を YYYY-MM-DD で返す（最初に見つかったもの）。"""
    m = re.search(r"(20\d{2})[年/](\d{1,2})[月/](\d{1,2})日?", text or "")
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


# ---------------------------------------------------------------- fetchers
def fetch_egov():
    """e-Gov パブリックコメント公式RSS → deadline テーマの素材。"""
    xml = fetch_text(EGOV_RSS)
    root = ET.fromstring(xml)
    records = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
        desc = re.sub(r"\s+", " ", desc).strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        records.append(
            {
                "source": "e-Govパブリックコメント",
                "title": title,
                "url": link,
                "date": find_dates(desc) or find_dates(pub),
                "text": (title + " " + desc)[:1500],
            }
        )
    log(f"e-Gov: {len(records)} records")
    return records


def fetch_diet():
    """衆議院の法案一覧ページ（ベストエフォート）。失敗したら例外で落とす。"""
    html = None
    for url in DIET_URL_CANDIDATES:
        try:
            html = fetch_text(url)
            break
        except Exception as e:  # noqa: BLE001 - try next candidate
            log(f"diet candidate failed {url}: {e}")
    if not html:
        raise RuntimeError("diet: all candidate URLs failed")
    text = re.sub(r"<[^>]+>", "\n", html)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
    records = []
    base = "https://www.shugiin.go.jp"
    links = re.findall(r'href="([^"]+)"[^>]*>([^<]*(?:法案|法律案)[^<]*)</a>', html)
    seen = set()
    for href, label in links:
        label = re.sub(r"\s+", " ", label).strip()
        if not label or label in seen:
            continue
        seen.add(label)
        url = href if href.startswith("http") else base + "/" + href.lstrip("/")
        records.append(
            {
                "source": "衆議院・議案一覧",
                "title": label,
                "url": url,
                "date": find_dates(label),
                "text": label[:800],
            }
        )
        if len(records) >= 15:
            break
    if not records:
        # フォールバック: 法案らしき行テキストだけ拾う
        for ln in lines:
            if ("法律案" in ln or "法案" in ln) and 8 <= len(ln) <= 120:
                records.append(
                    {
                        "source": "衆議院・議案一覧",
                        "title": ln,
                        "url": DIET_URL_CANDIDATES[0],
                        "date": find_dates(ln),
                        "text": ln[:800],
                    }
                )
                if len(records) >= 15:
                    break
    log(f"diet: {len(records)} records")
    return records


FETCHERS = [
    ("deadline", fetch_egov),
    ("diet", fetch_diet),
    # election / poll / life は信頼できる自動取得がまだ無いので前回データを保持する
]


# ---------------------------------------------------------------- gemini
def summarize_with_gemini(api_key, theme, records):
    """素材レコード群を、やさしい日本語の項目JSONリストに変換する。"""
    materials = "\n\n".join(
        f"- タイトル: {r['title']}\n  URL: {r['url']}\n  日付: {r.get('date') or '不明'}\n  本文抜粋: {r['text']}"
        for r in records
    )
    prompt = f"""あなたは、政治が苦手な人にも分かるようにニュースを噛み砕く編集者です。
次の素材（テーマ: {theme}）だけを使い、JSON配列だけを出力してください。説明文や```は付けないでください。

ルール（厳守）:
- 素材に書かれていない事実は絶対に作らない。推測で日付・数字・人名を補わない。
- 各項目の source_url は素材のURLをそのまま使う。
- summary は小学生高学年でも分かるやさしい日本語で2〜3文。専門用語は使わないか、glossary で必ず説明する。
- why は「なんで大事？」に一言〜二言で答える。
- changes は「決まるとどう変わる？」に答える。不明なら「まだ決まっていません」と書く。
- date は締切や実施日が素材から確実に分かる場合だけ "YYYY-MM-DD"、不明なら null。
- 賛否が素材に明記されている場合のみ pro / con を入れる。無ければ入れない。

出力形式（配列の各要素）:
{{"topic": "{theme}", "kind": "{theme}", "title": "短い見出し",
  "date": "YYYY-MM-DD" または null,
  "summary": "...", "why": "...", "changes": "...",
  "glossary": [["用語", "やさしい説明"]],
  "source_url": "素材のURL"}}

素材:
{materials}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }
    r = requests.post(
        GEMINI_URL,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def validate_items(items, allowed_urls, theme):
    """schema-ish 検証。1件でも不正なら全体を不採用にする。"""
    if not isinstance(items, list) or not items:
        raise ValueError("items is not a non-empty list")
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("item is not an object")
        for key in ("topic", "title", "summary", "why", "changes", "source_url"):
            if not isinstance(it.get(key), str) or not it[key].strip():
                raise ValueError(f"missing/empty field: {key}")
        if it["topic"] != theme or it["topic"] not in TOPICS:
            raise ValueError(f"unexpected topic: {it.get('topic')}")
        if it["source_url"] not in allowed_urls:
            raise ValueError(f"source_url not in fetched set: {it['source_url']}")
        d = it.get("date")
        if d is not None:
            if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", str(d)):
                raise ValueError(f"bad date format: {d}")
            datetime.strptime(d, "%Y-%m-%d")  # raises if invalid
        g = it.get("glossary", [])
        if not isinstance(g, list) or any(
            not (isinstance(x, list) and len(x) == 2) for x in g
        ):
            raise ValueError("bad glossary")
    return items


# ---------------------------------------------------------------- main
def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        log(
            "WARNING: GEMINI_API_KEY is not set. "
            "Skipping update; data.json left unchanged. "
            "(Set the GEMINI_API_KEY repository secret to enable nightly updates.)"
        )
        return 0

    if not os.path.exists(DATA_PATH):
        log("ERROR: data.json not found; aborting without changes.")
        return 1
    with open(DATA_PATH, encoding="utf-8") as f:
        current = json.load(f)

    candidate = copy.deepcopy(current)
    refreshed = []

    for theme, fetcher in FETCHERS:
        try:
            records = fetcher()
            if not records:
                log(f"{theme}: no records; keeping existing items")
                continue
            items = summarize_with_gemini(api_key, theme, records)
            items = validate_items(items, {r["url"] for r in records}, theme)
        except Exception as e:  # noqa: BLE001 - one source must not kill the run
            log(f"{theme}: FAILED ({e}); keeping existing items")
            continue
        others = [it for it in candidate["items"] if it.get("topic") != theme]
        candidate["items"] = others + items
        refreshed.append(theme)
        log(f"{theme}: refreshed with {len(items)} items")

    if not refreshed:
        log("No theme could be refreshed; leaving data.json unchanged.")
        return 0

    now = datetime.now(JST)
    candidate["fetched_at"] = now.isoformat(timespec="seconds")
    candidate["fetched_label"] = f"{now.month}月{now.day}日 {now:%H:%M} 時点"

    # 書き込み前に候補全体を検証してから、原子的（atomic）に差し替える
    tmp_path = DATA_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(candidate, f, ensure_ascii=False, indent=2)
    with open(tmp_path, encoding="utf-8") as f:
        json.load(f)  # parse check
    os.replace(tmp_path, DATA_PATH)
    log(f"data.json updated (themes: {', '.join(refreshed)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
