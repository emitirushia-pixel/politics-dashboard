#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政治ダッシュボード 夜間更新スクリプト

役割:
  - 公開情報源を取得する
      締切    : e-Gov パブリックコメント公式RSS
      国会    : 衆議院・参議院の議案ページから個別の法案（Phase 2）
      選挙    : Wikipedia「第21回統一地方選挙」から統一地方選の日程（Phase 2）
      世論調査: JNN（TBS NEWS DIG）の最新世論調査（Phase 2）
      暮らし  : 自動取得はせず、期限切れの項目だけを取り除く
  - GEMINI_API_KEY がある場合だけ、Gemini でやさしい日本語に要約して data.json を更新する
  - キーが無い場合は警告を出して正常終了し、data.json には一切触らない
  - 検証に失敗したら絶対に上書きしない（良いデータを守る）
  - 更新できなかったテーマの項目は前回のものをそのまま残す
  - 全体の項目数が減りすぎる場合は書き込み自体を中止する

依存: Python 標準ライブラリ + requests のみ
"""
import copy
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

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
SHUGIIN_MENU = "https://www.shugiin.go.jp/internet/itdb_gian.nsf/html/gian/menu.htm"
SANGIIN_GIAN = "https://www.sangiin.go.jp/japanese/joho1/kousei/gian/{session}/gian.htm"
ELECTION_WIKI = "https://ja.wikipedia.org/wiki/第21回統一地方選挙"
JNN_LIST = "https://newsdig.tbs.co.jp/list/feature/JNN%E4%B8%96%E8%AB%96%E8%AA%BF%E6%9F%BB"
JNN_TAG_LIST = "https://newsdig.tbs.co.jp/list/tag/%E4%B8%96%E8%AB%96%E8%AA%BF%E6%9F%BB"

# 法案の「暮らしへの近さ」を測るキーワード（選定の優先度付け用）
DIET_KEYWORDS = [
    "消費税", "所得税", "住民税", "社会保険", "年金", "医療", "介護", "子育て",
    "子ども", "教育", "物価", "賃金", "税", "電気", "ガス", "ガソリン", "燃料",
    "住宅", "中小企業", "選挙", "定数", "政治資金", "防衛", "安全保障", "外国人",
    "入管", "災害", "復興", "地方", "負担軽減",
]
POLL_MAX_AGE_DAYS = 45
MIN_TOTAL_ITEMS = 18


def log(msg):
    print(f"[update] {msg}", flush=True)


def today_jst():
    return datetime.now(JST).date()


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


def wareki_to_iso(text):
    """「令和8年10月9日」のような和暦表記を ISO 日付にする。"""
    m = re.search(r"(令和|平成)\s*(\d+|元)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text or "")
    if not m:
        return None
    era, y, mo, d = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
    y = 1 if y == "元" else int(y)
    year = (2018 + y) if era == "令和" else (1988 + y)
    try:
        return date(year, mo, d).isoformat()
    except ValueError:
        return None


def parse_item_date(value):
    """項目の date 欄（'YYYY-MM-DD' や 'YYYY-MM-DDT23:59'）を date にする。"""
    if not isinstance(value, str):
        return None
    m = re.match(r"(20\d{2})-(\d{2})-(\d{2})", value)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def strip_tags(html):
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


class TableParser(HTMLParser):
    """<table> の行をセル（テキスト＋リンク）として集め、h2 見出しも記録する。"""

    def __init__(self):
        super().__init__()
        self.events = []  # ("h2", text) | ("row", [cell, ...])
        self._row = None
        self._cell = None
        self._h2 = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"t": "", "links": []}
        elif tag == "a" and self._cell is not None:
            href = dict(attrs).get("href")
            if href:
                self._cell["links"].append(href)
        elif tag == "h2":
            self._h2 = ""

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["t"] += data
        if self._h2 is not None:
            self._h2 += data

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._cell["t"] = re.sub(r"\s+", " ", self._cell["t"]).strip()
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.events.append(("row", self._row))
            self._row = None
        elif tag == "h2" and self._h2 is not None:
            text = re.sub(r"\s+", " ", self._h2).strip()
            if text:
                self.events.append(("h2", text))
            self._h2 = None


def parse_tables(html):
    p = TableParser()
    p.feed(html)
    return p.events


# ---------------------------------------------------------------- fetchers
def _localname(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_egov(xml):
    """RSS 1.0(RDF) / 2.0 どちらでも、名前空間に依存せず item を拾う。"""
    root = ET.fromstring(xml)
    records = []
    for item in root.iter():
        if _localname(item.tag) != "item":
            continue
        fields = {}
        for child in item:
            fields.setdefault(_localname(child.tag), (child.text or "").strip())
        title = fields.get("title", "")
        link = fields.get("link", "")
        desc = re.sub(r"<[^>]+>", " ", fields.get("description", ""))
        desc = re.sub(r"\s+", " ", desc).strip()
        pub = fields.get("pubDate", "") or fields.get("date", "")
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
    return records


def fetch_egov():
    """e-Gov パブリックコメント公式RSS → deadline テーマの素材。

    フィードは件数が多いので、締切が近い順に最大12件へ絞る
    （日付が取れないものは後ろに回す）。
    """
    records = _parse_egov(fetch_text(EGOV_RSS))
    records.sort(key=lambda r: (r.get("date") is None, r.get("date") or ""))
    records = records[:12]
    log(f"e-Gov: {len(records)} records")
    return records


# ---------------------------------------------------------------- diet
SHUGIIN_TYPE_BY_CODE = {"05": "衆法", "06": "参法", "09": "閣法"}
SANGIIN_SECTION_TYPE = {
    "法律案（内閣提出）一覧": "閣法",
    "法律案（衆法）一覧": "衆法",
    "法律案（参法）一覧": "参法",
}


def diet_score(title):
    return sum(1 for kw in DIET_KEYWORDS if kw in title)


def _bill_material(bill):
    parts = [
        f"議案件名: {bill['title']}",
        f"種別: {bill['type']}（第{bill['session']}回国会 第{bill['number']}号）",
    ]
    if bill.get("status"):
        parts.append(f"審議状況: {bill['status']}")
    if bill.get("detail"):
        parts.append(bill["detail"])
    return "\n".join(parts)[:1500]


def fetch_bill_detail(bill):
    """経過情報／明細ページから提出日・提出者・所管官庁などを拾う（失敗しても無視）。"""
    try:
        text = strip_tags(fetch_text(bill["url"]))
    except Exception as e:  # noqa: BLE001
        log(f"diet detail failed {bill['title'][:20]}: {e}")
        return
    bits = []
    m = re.search(
        r"(?:提出日|議案受理年月日|衆議院議案受理年月日|参議院議案受理年月日)\s*"
        r"(?:令和|平成)\s*(?:\d+|元)\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日",
        text,
    )
    if m:
        iso = wareki_to_iso(m.group(0))
        bits.append(f"提出・受理日: {m.group(0)}" + (f"（{iso}）" if iso else ""))
    m = re.search(r"議案提出者\s*(\S{1,30})", text)
    if m:
        bits.append(f"提出者: {m.group(1)}")
    m = re.search(r"議案提出会派\s*(\S{1,30})", text)
    if m:
        bits.append(f"提出会派: {m.group(1)}")
    m = re.search(r"所管官庁\s*(.{2,60}?)(?:議案等|ファイル|$)", text)
    if m:
        bits.append(f"所管官庁: {m.group(1).strip()}")
    bill["detail"] = " / ".join(bits)


def fetch_diet():
    """衆議院＋参議院の議案ページから、個別の法案を最大6件選んで素材にする。"""
    html = fetch_text(SHUGIIN_MENU)
    m = re.search(r"第(\d+)回国会", strip_tags(html))
    if not m:
        raise RuntimeError("diet: cannot determine current session from shugiin")
    session = int(m.group(1))

    # --- 衆議院: 本文ファイル名の種別コードで法律案だけを拾う
    shu_bills = []
    for kind, row in parse_tables(html):
        if kind != "row" or len(row) < 6:
            continue
        honbun = [l for l in row[5]["links"] if "honbun" in l]
        keika = [l for l in row[4]["links"] if "keika" in l]
        if not honbun or not keika:
            continue
        cm = re.search(r"g\d{3}(\d{2})\d{3}\.htm", honbun[0])
        if not cm or cm.group(1) not in SHUGIIN_TYPE_BY_CODE:
            continue
        try:
            sess_no, number = int(row[0]["t"]), int(row[1]["t"])
        except ValueError:
            continue
        title = row[2]["t"]
        if "法律案" not in title:
            continue
        shu_bills.append(
            {
                "source": "衆議院・議案情報",
                "title": title,
                "type": SHUGIIN_TYPE_BY_CODE[cm.group(1)],
                "session": sess_no,
                "number": number,
                "status": row[3]["t"],
                "url": urljoin(SHUGIIN_MENU, keika[0]),
                "date": None,
            }
        )

    # --- 参議院: h2 見出しでセクションを判定する
    san_bills = []
    gian_url = SANGIIN_GIAN.format(session=session)
    try:
        s_html = fetch_text(gian_url)
        section = None
        for kind, payload in parse_tables(s_html):
            if kind == "h2":
                section = SANGIIN_SECTION_TYPE.get(payload)
                continue
            if kind != "row" or section is None or len(payload) < 3:
                continue
            meisai = [l for l in payload[2]["links"] if "meisai" in l]
            if not meisai:
                continue
            try:
                sess_no, number = int(payload[0]["t"]), int(payload[1]["t"])
            except ValueError:
                continue
            title = payload[2]["t"]
            if "法律案" not in title:
                continue
            san_bills.append(
                {
                    "source": "参議院・議案情報",
                    "title": title,
                    "type": section,
                    "session": sess_no,
                    "number": number,
                    "status": None,
                    "url": urljoin(gian_url, meisai[0]),
                    "date": None,
                }
            )
    except Exception as e:  # noqa: BLE001 - 参議院が取れなくても衆議院分で続行
        log(f"diet: sangiin fetch failed ({e}); continuing with shugiin only")

    if not shu_bills and not san_bills:
        raise RuntimeError("diet: no bills parsed from either house")

    def sort_key(b):
        return (-diet_score(b["title"]), b["number"])

    kaku = sorted(
        [b for b in san_bills if b["type"] == "閣法" and b["session"] == session],
        key=sort_key,
    )
    san = sorted(
        [b for b in san_bills if b["type"] == "参法" and b["session"] == session],
        key=sort_key,
    )
    shu = sorted(
        [
            b
            for b in shu_bills
            if b["type"] == "衆法"
            and b["session"] == session
            and "撤回" not in b["status"]
            and "廃案" not in b["status"]
        ],
        key=sort_key,
    )
    continued = sorted(
        [
            b
            for b in shu_bills
            if b["session"] == session - 1
            and "撤回" not in b["status"]
            and "廃案" not in b["status"]
        ],
        key=sort_key,
    )

    picked, seen = [], set()

    def take(pool, n):
        for b in pool:
            if n <= 0 or len(picked) >= 6:
                return
            if b["title"] in seen:
                continue
            seen.add(b["title"])
            picked.append(b)
            n -= 1

    take(kaku, 3)
    take(shu, 2)
    take(san, 1)
    take(kaku + shu + san + continued, 6)  # 残り枠をスコア順に埋める

    if len(picked) < 3:
        raise RuntimeError(f"diet: only {len(picked)} bills selected")

    for b in picked:
        fetch_bill_detail(b)
        b["text"] = _bill_material(b)
    log(f"diet: {len(picked)} bills selected (session {session})")
    return picked


# ---------------------------------------------------------------- election
def fetch_election():
    """Wikipedia「第21回統一地方選挙」から前半戦・後半戦の日付を検証して素材にする。"""
    html = fetch_text(ELECTION_WIKI)
    text = strip_tags(html)
    m = re.search(
        r"(20\d{2})\s*年\s*（[^）]*）\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*と\s*同月"
        r"\s*(\d{1,2})\s*日\s*の\s*2回に分けて執行",
        text,
    )
    if not m:
        raise RuntimeError("election: unified election dates not found on wiki page")
    year, mo, d1, d2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    first, second = date(year, mo, d1), date(year, mo, d2)
    today = today_jst()
    if not (today < first < second):
        raise RuntimeError(f"election: dates not in future order: {first} / {second}")
    if "石川" not in text:
        raise RuntimeError("election: Ishikawa not mentioned on wiki page")

    common = (
        f"統一地方選挙は{year}年{mo}月{d1}日（前半戦）と同月{d2}日（後半戦）の"
        "2回に分けて行われる。前半戦は道府県の知事・議員と政令指定都市の市長・議員、"
        "後半戦はそれ以外の市・特別区・町村の首長・議員の選挙。"
    )
    records = [
        {
            "source": "Wikipedia・第21回統一地方選挙",
            "title": f"{year}年統一地方選挙（道府県・政令指定都市の首長・議員選挙）",
            "url": ELECTION_WIKI,
            "date": first.isoformat(),
            "text": common + "前半戦の投票日。",
        },
        {
            "source": "Wikipedia・第21回統一地方選挙",
            "title": "石川県議会議員選挙",
            "url": ELECTION_WIKI,
            "date": first.isoformat(),
            "text": common + "石川県議会議員選挙も前半戦の日程で行われる。",
        },
        {
            "source": "Wikipedia・第21回統一地方選挙",
            "title": f"{year}年統一地方選挙（政令市以外の市・特別区・町村の首長・議員選挙）",
            "url": ELECTION_WIKI,
            "date": second.isoformat(),
            "text": common + "後半戦の投票日。",
        },
        {
            "source": "Wikipedia・第21回統一地方選挙",
            "title": "石川県内の市長選挙・町長選挙・市議会議員選挙など",
            "url": ELECTION_WIKI,
            "date": second.isoformat(),
            "text": common + "石川県内の市町でも後半戦の日程で選挙が行われる。",
        },
    ]
    log(f"election: unified dates {first} / {second}")
    return records


# ---------------------------------------------------------------- poll (JNN)
def _jnn_title_support(html):
    """記事HTMLのタイトルから (支持率, 速報かどうか) を返す。対象外なら (None, False)。"""
    title_m = re.search(r"<title>(.*?)</title>", html, re.S)
    title = re.sub(r"\s+", " ", title_m.group(1)) if title_m else ""
    if "JNN世論調査" not in title:
        return None, False
    sm = re.search(r"支持率[はが]?\s*(\d{1,2}(?:\.\d+)?)\s*[%％]", title)
    if not sm:
        return None, False
    return float(sm.group(1)), ("速報" in title)


def _jnn_article_facts(article_id, html, support):
    """JNN世論調査記事から 不支持率・調査日 を抽出する。揃わなければ None。"""
    url = f"https://newsdig.tbs.co.jp/articles/-/{article_id}"

    text = strip_tags(html)
    # 「支持しないと答えた人は、先月からXポイント下落して33.9%でした」のように
    # 途中にポイント変動の数字が挟まるため、文節内の最後の % を不支持率とする。
    oppose = None
    for pat in (
        r"「支持しない」と答えた人は([^。]{0,100})",
        r"支持しないと答えた人は([^。]{0,100})",
        r"「支持できない」と答えた人は([^。]{0,100})",
    ):
        seg = re.search(pat, text)
        if seg:
            pcts = re.findall(r"(\d{1,2}(?:\.\d+)?)\s*[%％]", seg.group(1))
            if pcts:
                oppose = float(pcts[-1])
                break
    if oppose is None:
        return None
    if not (0 < support < 100 and 0 < oppose < 100 and support + oppose <= 100.5):
        return None

    pm = re.search(r'"datePublished"\s*:\s*"(\d{4})-(\d{2})-(\d{2})', html)
    if not pm:
        return None
    published = date(int(pm.group(1)), int(pm.group(2)), int(pm.group(3)))

    survey = None
    for page_url in (url + "?page=2", url):
        try:
            t2 = strip_tags(fetch_text(page_url))
        except Exception:  # noqa: BLE001
            continue
        dm = re.search(
            r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*（.）\s*、\s*(\d{1,2})\s*日\s*（.）"
            r"\s*に\s*、\s*全国",
            t2,
        )
        if dm:
            smon, sday, eday = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
            syear = published.year - (1 if smon > published.month else 0)
            try:
                survey = (date(syear, smon, sday), date(syear, smon, eday))
            except ValueError:
                survey = None
            break
    if survey is None:
        return None
    start, end = survey
    if not (start <= end <= published + timedelta(days=1)):
        return None
    if (today_jst() - end).days > POLL_MAX_AGE_DAYS or end > today_jst():
        return None
    return {
        "url": url,
        "support": support,
        "oppose": oppose,
        "survey_start": start,
        "survey_end": end,
        "published": published,
    }


def fetch_poll():
    """JNN（TBS）の最新世論調査を1件、検証済みの数字つきで素材にする。"""
    ids = []
    for list_url in (JNN_TAG_LIST, JNN_LIST):
        try:
            html = fetch_text(list_url)
        except Exception as e:  # noqa: BLE001
            log(f"poll: list fetch failed {list_url}: {e}")
            continue
        for aid in re.findall(r"articles/-/(\d+)", html):
            if aid not in ids:
                ids.append(aid)
    if not ids:
        raise RuntimeError("poll: no candidate articles found")

    # 速報タイトルを優先し、無ければ見出し型タイトルの記事から探す（2パス）
    cache = {}

    def article_html(aid):
        if aid not in cache:
            cache[aid] = fetch_text(f"https://newsdig.tbs.co.jp/articles/-/{aid}")
        return cache[aid]

    facts = None
    for want_sokuhou in (True, False):
        for aid in ids[:30]:
            try:
                support, is_sokuhou = _jnn_title_support(article_html(aid))
                if support is None or (want_sokuhou and not is_sokuhou):
                    continue
                facts = _jnn_article_facts(aid, cache[aid], support)
            except Exception as e:  # noqa: BLE001
                log(f"poll: article {aid} failed ({e})")
                facts = None
            if facts:
                break
        if facts:
            break
    if not facts:
        raise RuntimeError("poll: no JNN article with complete verified figures")

    s, e = facts["survey_start"], facts["survey_end"]
    if s.month == e.month and s.day != e.day:
        when = f"{s.year}年{s.month}月{s.day}・{e.day}日"
    elif s == e:
        when = f"{s.year}年{s.month}月{s.day}日"
    else:
        when = f"{s.year}年{s.month}月{s.day}日〜{e.month}月{e.day}日"
    record = {
        "source": "JNN世論調査（TBS NEWS DIG）",
        "title": f"JNNの世論調査（{when}）",
        "url": facts["url"],
        "date": None,
        "text": (
            f"調査主体: JNN（TBSのニュースネットワーク）\n"
            f"調査日: {s.isoformat()} 〜 {e.isoformat()}\n"
            f"今の内閣を支持する人: {facts['support']}%\n"
            f"今の内閣を支持しない人: {facts['oppose']}%"
        ),
        "poll_fact": {
            "agency": "JNN",
            "support": facts["support"],
            "oppose": facts["oppose"],
            "surveyed_on": e.isoformat(),
        },
    }
    log(f"poll: JNN {e} support={facts['support']} oppose={facts['oppose']}")
    return [record]


FETCHERS = [
    ("deadline", fetch_egov),
    ("diet", fetch_diet),
    ("election", fetch_election),
    ("poll", fetch_poll),
    # life は信頼できる自動取得が無いので前回データを保持し、期限切れだけ落とす
]

THEME_EXTRA_RULES = {
    "diet": "- title は素材の議案件名を一字一句そのまま使う（短くしたり言い換えたりしない）。\n"
    "- date は null にする。",
    "election": "- title と date は素材の見出しと日付を一字一句そのまま使う。",
    "poll": "- title は素材の見出しを一字一句そのまま使う。\n"
    '- 必ず "poll": {"agency": "JNN", "support": 素材の「支持する人」の数字, '
    '"oppose": 素材の「支持しない人」の数字, "surveyed_on": "素材の調査最終日YYYY-MM-DD"} '
    "を入れ、数字は素材のとおりに写す。",
}


# ---------------------------------------------------------------- gemini
def gemini_json(api_key, prompt):
    """Gemini に JSON 応答を求める共通処理。"""
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


def summarize_with_gemini(api_key, theme, records):
    """素材レコード群を、やさしい日本語の項目JSONリストに変換する。"""
    materials = "\n\n".join(
        f"- タイトル: {r['title']}\n  URL: {r['url']}\n  日付: {r.get('date') or '不明'}\n  本文抜粋: {r['text']}"
        for r in records
    )
    extra = THEME_EXTRA_RULES.get(theme, "")
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
{extra}

出力形式（配列の各要素）:
{{"topic": "{theme}", "kind": "{theme}", "title": "短い見出し",
  "date": "YYYY-MM-DD" または null,
  "summary": "...", "why": "...", "changes": "...",
  "glossary": [["用語", "やさしい説明"]],
  "source_url": "素材のURL"}}

素材:
{materials}
"""
    return gemini_json(api_key, prompt)


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


# ------------------------------------------------------- theme-specific checks
CATEGORY_LABEL_RE = re.compile(r"^(衆法|参法|閣法)[（(]")


def check_diet_items(items, records):
    """国会テーマの追加検証: 実在の法案名だけが並んでいることを保証する。"""
    allowed_titles = {r["title"] for r in records}
    if len(items) < 3:
        raise ValueError(f"diet: only {len(items)} items")
    for it in items:
        t = it["title"]
        if t not in allowed_titles:
            raise ValueError(f"diet: title not a fetched bill: {t}")
        if "法律案" not in t or CATEGORY_LABEL_RE.match(t) or "について" in t:
            raise ValueError(f"diet: title looks like a category label: {t}")
        it["date"] = None  # 提出日は並び順に使わないので日付欄は空に統一
    return items


def check_election_items(items, records):
    """選挙テーマの追加検証: 見出し・投票日・出典が素材と完全一致すること。"""
    expect = {r["title"]: r["date"] for r in records}
    if len(items) != len(records):
        raise ValueError("election: item count differs from records")
    today = today_jst()
    for it in items:
        if it["title"] not in expect:
            raise ValueError(f"election: unexpected title: {it['title']}")
        if it.get("date") != expect[it["title"]]:
            raise ValueError(f"election: date mismatch for {it['title']}")
        d = parse_item_date(it["date"])
        if d is None or d < today:
            raise ValueError(f"election: date not in future: {it['date']}")
    return items


def _as_float(v):
    """数値でも "63.0%" のような文字列でも float にする。できなければ None。"""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().rstrip("%％").strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


def check_poll_items(items, records):
    """世論調査テーマの追加検証: 数字が抽出事実と一致すること。"""
    if len(records) != 1 or len(items) != 1:
        raise ValueError("poll: expected exactly one record and one item")
    fact = records[0]["poll_fact"]
    it = items[0]
    if it["title"] != records[0]["title"]:
        raise ValueError("poll: title mismatch")
    p = it.get("poll")
    if not isinstance(p, dict):
        raise ValueError("poll: missing poll object")
    if p.get("agency") != fact["agency"]:
        raise ValueError("poll: agency mismatch")
    for key in ("support", "oppose"):
        v = _as_float(p.get(key))
        if v is None or abs(v - fact[key]) > 0.051:
            raise ValueError(f"poll: {key} mismatch: {p.get(key)} != {fact[key]}")
        p[key] = v  # 表示用に数値へ正規化する
    if p.get("surveyed_on") != fact["surveyed_on"]:
        raise ValueError("poll: surveyed_on mismatch")
    it["date"] = None
    return items


THEME_CHECKS = {
    "diet": check_diet_items,
    "election": check_election_items,
    "poll": check_poll_items,
}


def merge_poll_items(existing, new_items):
    """新しいJNN項目を既存の世論調査に差し込む（同機関は置換、古いものは除く）。"""
    today = today_jst()
    kept = []
    for it in existing:
        p = it.get("poll") or {}
        agency = str(p.get("agency", ""))
        if "JNN" in agency or "TBS" in agency:
            continue  # 新しいJNN項目に置き換える
        surveyed = parse_item_date(p.get("surveyed_on"))
        if surveyed is not None and (today - surveyed).days > POLL_MAX_AGE_DAYS:
            continue  # 45日より古い調査は一覧から外す
        kept.append(it)
    merged = kept + list(new_items)
    merged.sort(
        key=lambda it: (it.get("poll") or {}).get("surveyed_on") or "",
        reverse=True,
    )
    return merged[:5]


# ---------------------------------------------------------------- digest
def build_digest(api_key, items):
    """最終項目セットから「今週の政治、3行で」を作り直す。"""
    materials = "\n\n".join(
        f"- [{it['topic']}] {it['title']}（{it.get('date') or '日付なし'}）\n"
        f"  要約: {it['summary']}\n  URL: {it['source_url']}"
        for it in items
    )
    allowed = {it["source_url"] for it in items}
    prompt = f"""あなたは、政治が苦手な人にも分かるようにニュースを噛み砕く編集者です。
次の項目一覧だけを使い、「今週の政治、3行で」の要約をJSON配列だけで出力してください。説明文や```は付けないでください。

ルール（厳守）:
- ちょうど3件のオブジェクトを返す。各 text は1文で、60文字以内を目安に短く。
- 一覧に書かれていない事実は絶対に作らない。
- 各オブジェクトの source_url は、その行の根拠にした項目のURLをそのまま使う。

出力形式:
[{{"text": "...", "source_url": "..."}}, {{"text": "...", "source_url": "..."}}, {{"text": "...", "source_url": "..."}}]

項目一覧:
{materials}
"""
    digest = gemini_json(api_key, prompt)
    if not isinstance(digest, list) or len(digest) != 3:
        raise ValueError("digest: not exactly 3 lines")
    for line in digest:
        if not isinstance(line, dict):
            raise ValueError("digest: line is not an object")
        t = line.get("text")
        if not isinstance(t, str) or not (10 <= len(t.strip()) <= 130):
            raise ValueError("digest: line length out of range")
        if line.get("source_url") not in allowed:
            raise ValueError("digest: source_url not in item set")
    return digest


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
            check = THEME_CHECKS.get(theme)
            if check:
                items = check(items, records)
        except Exception as e:  # noqa: BLE001 - one source must not kill the run
            log(f"{theme}: FAILED ({e}); keeping existing items")
            continue
        if theme == "poll":
            existing = [it for it in candidate["items"] if it.get("topic") == "poll"]
            merged = merge_poll_items(existing, items)
            others = [it for it in candidate["items"] if it.get("topic") != "poll"]
            candidate["items"] = others + merged
            log(f"poll: merged; theme now has {len(merged)} items")
        else:
            others = [it for it in candidate["items"] if it.get("topic") != theme]
            candidate["items"] = others + items
        refreshed.append(theme)
        log(f"{theme}: refreshed with {len(items)} items")

    # 暮らしテーマ: 日付が明確に過去の項目だけを取り除く
    today = today_jst()
    before = len(candidate["items"])
    candidate["items"] = [
        it
        for it in candidate["items"]
        if it.get("topic") != "life"
        or (lambda d: d is None or d >= today)(parse_item_date(it.get("date")))
    ]
    life_dropped = before - len(candidate["items"])
    if life_dropped:
        log(f"life: dropped {life_dropped} expired item(s)")

    if not refreshed and not life_dropped:
        log("No theme could be refreshed; leaving data.json unchanged.")
        return 0

    # 3行まとめは最終項目セットから作り直す（失敗したら前回のまとめを残す）
    try:
        candidate["digest"] = build_digest(api_key, candidate["items"])
        log("digest: regenerated")
    except Exception as e:  # noqa: BLE001
        log(f"digest: FAILED ({e}); keeping previous digest")

    # 全体ガード: 項目が減りすぎていたり、空のテーマがあれば書き込まない
    counts = {t: 0 for t in TOPICS}
    for it in candidate["items"]:
        if it.get("topic") in counts:
            counts[it["topic"]] += 1
    if len(candidate["items"]) < MIN_TOTAL_ITEMS or any(
        v == 0 for v in counts.values()
    ):
        log(
            f"ABORT: global guard failed (total={len(candidate['items'])}, "
            f"counts={counts}); data.json left unchanged."
        )
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
