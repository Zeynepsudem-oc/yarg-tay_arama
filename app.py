# -*- coding: utf-8 -*-
"""
Yargıtay 11. Hukuk Dairesi Karar Arama Aracı
=============================================

Bu program, https://karararama.yargitay.gov.tr sitesinin arka planda kullandığı
resmi arama servislerine (aynı sitenin kendi arayüzünün kullandığı uç noktalar)
istek göndererek 11. Hukuk Dairesi kararları arasında anahtar kelime araması yapar
ve bulunan kararların tam metnini otomatik olarak getirir.

ÇALIŞTIRMA
----------
1) Gerekli paketleri kurun:
        pip install -r requirements.txt

2) Programı başlatın:
        python app.py

3) Program otomatik olarak tarayıcınızda şu adresi açacaktır:
        http://127.0.0.1:5000
"""

import os
import re
import time
import csv
import json
import math
import hashlib
import sqlite3
import logging
import threading
import webbrowser
from io import BytesIO, StringIO
from datetime import datetime, timezone

from flask import Flask, request, jsonify, render_template_string, send_file
import requests
from bs4 import BeautifulSoup
from fpdf import FPDF

# ----------------------------------------------------------------------------
# Ayarlar
# ----------------------------------------------------------------------------

DEBUG = True

BASE_URL = "https://karararama.yargitay.gov.tr"
SEARCH_URL = f"{BASE_URL}/aramadetaylist"
DOCUMENT_URL = f"{BASE_URL}/getDokuman"

DAIRE_ADI = "11. Hukuk Dairesi"
PAGE_SIZE = 10  # Her sayfada gösterilecek sonuç sayısı
REQUEST_TIMEOUT = 45  # saniye — resmi site zaman zaman yavaş cevap verebiliyor

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "Referer": BASE_URL + "/",
    "Origin": BASE_URL,
}

# Kanun kısaltmaları -> madde referanslarını yakalamak için kullanılan regex.
KANUN_KISALTMALARI = [
    "TTK", "TMK", "TBK", "HMK", "İİK", "TCK", "CMK", "SMK", "FSEK",
    "KVKK", "TKHK", "İK", "VUK", "AY", "TBMM", "TSK",
]

logging.basicConfig(level=logging.INFO if not DEBUG else logging.DEBUG)
logger = logging.getLogger("yargitay-arama")

app = Flask(__name__)

_session = None


def get_session():
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get(BASE_URL + "/", timeout=15)
        except requests.RequestException as e:
            logger.warning("Ana sayfa ziyaret edilemedi: %s", e)
        _session = s
    return _session


# ----------------------------------------------------------------------------
# Yerel önbellekleme (SQLite)
# ----------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "cache.db")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS search_cache (
            cache_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS decision_cache (
            doc_id TEXT PRIMARY KEY,
            html TEXT,
            text TEXT,
            text_marked TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            fav_key TEXT PRIMARY KEY,
            esas_no TEXT,
            karar_no TEXT,
            tarih TEXT,
            daire TEXT,
            text TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def make_search_cache_key(keyword, or_keyword, page_number, page_size, filters):
    key_obj = {
        "keyword": keyword,
        "orKeyword": or_keyword,
        "page": page_number,
        "pageSize": page_size,
        "filters": filters,
    }
    raw = json.dumps(key_obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cached_search(cache_key):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT payload_json, created_at FROM search_cache WHERE cache_key = ?",
        (cache_key,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None, None
    try:
        return json.loads(row[0]), row[1]
    except ValueError:
        return None, None


def save_cached_search(cache_key, payload_dict):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO search_cache (cache_key, payload_json, created_at) VALUES (?, ?, ?)",
        (cache_key, json.dumps(payload_dict, ensure_ascii=False), _now_iso()),
    )
    conn.commit()
    conn.close()


def get_cached_decision(doc_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT html, text, text_marked, created_at FROM decision_cache WHERE doc_id = ?",
        (str(doc_id),),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"html": row[0], "text": row[1], "textMarked": row[2], "createdAt": row[3]}


def save_cached_decision(doc_id, html_fragment, text, text_marked):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO decision_cache (doc_id, html, text, text_marked, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(doc_id), html_fragment, text, text_marked, _now_iso()),
    )
    conn.commit()
    conn.close()


def clear_cache():
    """Tüm önbelleği temizler."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM search_cache")
    cur.execute("DELETE FROM decision_cache")
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# Favoriler (yerel SQLite'ta kalıcı liste)
# ----------------------------------------------------------------------------

def add_favorite(fav_key, esas_no, karar_no, tarih, daire, text):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """INSERT OR REPLACE INTO favorites
           (fav_key, esas_no, karar_no, tarih, daire, text, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (fav_key, esas_no or "", karar_no or "", tarih or "", daire or DAIRE_ADI, text or "", _now_iso()),
    )
    conn.commit()
    conn.close()


def remove_favorite(fav_key):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM favorites WHERE fav_key = ?", (fav_key,))
    conn.commit()
    conn.close()


def list_favorites():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT fav_key, esas_no, karar_no, tarih, daire, text, created_at "
        "FROM favorites ORDER BY created_at DESC"
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "favKey": r[0],
            "esasNo": r[1],
            "kararNo": r[2],
            "tarih": r[3],
            "daire": r[4],
            "text": r[5],
            "createdAt": r[6],
        }
        for r in rows
    ]


def list_favorite_keys():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT fav_key FROM favorites")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


init_db()


# ----------------------------------------------------------------------------
# Arama yardımcı fonksiyonları
# ----------------------------------------------------------------------------

def build_search_payload(
    keyword,
    page_number=1,
    page_size=PAGE_SIZE,
    esas_yil="",
    esas_ilk_sira="",
    esas_son_sira="",
    karar_yil="",
    karar_ilk_sira="",
    karar_son_sira="",
    baslangic_tarihi="",
    bitis_tarihi="",
):
    return {
        "data": {
            "arananKelime": keyword,
            "hukuk": DAIRE_ADI,
            "esasYil": esas_yil or "",
            "esasIlkSiraNo": esas_ilk_sira or "",
            "esasSonSiraNo": esas_son_sira or "",
            "kararYil": karar_yil or "",
            "kararIlkSiraNo": karar_ilk_sira or "",
            "kararSonSiraNo": karar_son_sira or "",
            "baslangicTarihi": baslangic_tarihi or "",
            "bitisTarihi": bitis_tarihi or "",
            "siralama": "3",
            "siralamaDirection": "desc",
            "birimYrgKurulDaire": "",
            "birimYrgHukukDaire": DAIRE_ADI,
            "birimYrgCezaDaire": "",
            "pageNumber": page_number,
            "pageSize": page_size,
        }
    }


def extract_records(raw_json):
    def find_list(obj):
        if isinstance(obj, dict):
            if "data" in obj and isinstance(obj["data"], list):
                return obj["data"]
            if "data" in obj and isinstance(obj["data"], dict):
                return find_list(obj["data"])
        if isinstance(obj, list):
            return obj
        return None

    records = find_list(raw_json)

    total = None
    if isinstance(raw_json, dict):
        for key in ("recordsTotal", "recordsFiltered", "totalRecords", "total"):
            inner = raw_json.get("data", raw_json)
            if isinstance(inner, dict) and key in inner:
                total = inner[key]
                break

    return records or [], total


def guess_id(record):
    for key in ("id", "kararId", "documentId", "belgeId", "dokumanId"):
        if key in record and record[key]:
            return record[key]
    return None


def html_to_text(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text


def do_single_search(keyword, page_number, page_size, filters):
    session = get_session()
    payload = build_search_payload(
        keyword,
        page_number,
        page_size,
        esas_yil=filters.get("esasYil", ""),
        esas_ilk_sira=filters.get("esasIlkSiraNo", ""),
        esas_son_sira=filters.get("esasSonSiraNo", ""),
        karar_yil=filters.get("kararYil", ""),
        karar_ilk_sira=filters.get("kararIlkSiraNo", ""),
        karar_son_sira=filters.get("kararSonSiraNo", ""),
        baslangic_tarihi=filters.get("baslangicTarihi", ""),
        bitis_tarihi=filters.get("bitisTarihi", ""),
    )

    try:
        resp = session.post(SEARCH_URL, data=json.dumps(payload), timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        raise RuntimeError(
            "Yargıtay sunucusu şu anda yanıt vermiyor (zaman aşımı). "
            "Sunucu yoğun olabilir, birkaç dakika sonra tekrar deneyin."
        )

    if DEBUG:
        logger.debug("Arama HTTP durumu: %s", resp.status_code)
        logger.debug("Arama ham cevap (ilk 2000 karakter): %s", resp.text[:2000])

    if resp.status_code != 200:
        raise RuntimeError("Yargıtay sunucusu {} kodu döndürdü.".format(resp.status_code))

    try:
        raw_json = resp.json()
    except ValueError:
        raise RuntimeError("Sunucu cevabı JSON formatında değil (site yapısı değişmiş olabilir).")

    records, total = extract_records(raw_json)

    results = []
    for rec in records:
        results.append({"id": guess_id(rec), "raw": rec})

    return results, total


def dedupe_results(list_of_result_lists):
    seen = set()
    merged = []
    for results in list_of_result_lists:
        for r in results:
            raw = r.get("raw") or {}
            key = r.get("id") or (
                str(raw.get("esasNo") or raw.get("esas_no") or "")
                + "|"
                + str(raw.get("kararNo") or raw.get("karar_no") or "")
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)
    return merged


# ----------------------------------------------------------------------------
# Madde/kanun referansı işaretleme
# ----------------------------------------------------------------------------

def _build_madde_regex():
    kisaltmalar = "|".join(re.escape(k) for k in KANUN_KISALTMALARI)
    pattern = (
        r"(?:\b(?:" + kisaltmalar + r")\b\s*(?:'\w*\s*)?"
        r"(?:m\.?|madde|md\.?)\s*\d+[/\.]?\d*)"
        r"|(?:\d{3,4}\s+say[ıi]l[ıi]\s+(?:Kanun|Yönetmelik)(?:'\w*)?\s+"
        r"\d+\.?\s*maddesi)"
    )
    return re.compile(pattern, flags=re.IGNORECASE)


MADDE_REGEX = _build_madde_regex()


def isaretle_madde_referanslari(text):
    if not text:
        return ""

    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    def _wrap(match):
        return '<mark class="madde-ref">' + match.group(0) + "</mark>"

    return MADDE_REGEX.sub(_wrap, escaped)


# ----------------------------------------------------------------------------
# CSV / PDF üretimi
# ----------------------------------------------------------------------------

CSV_FIELDNAMES = ["esasNo", "kararNo", "tarih", "daire", "text"]


def entries_to_csv_bytes(entries):
    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDNAMES, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for e in entries:
        writer.writerow({
            "esasNo": e.get("esasNo", "") or "",
            "kararNo": e.get("kararNo", "") or "",
            "tarih": e.get("tarih", "") or "",
            "daire": e.get("daire", DAIRE_ADI) or DAIRE_ADI,
            "text": e.get("text", "") or "",
        })
    return buf.getvalue().encode("utf-8-sig")


def csv_bytes_to_entries(csv_bytes):
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(StringIO(text))
    return [dict(row) for row in reader]


FONT_REGULAR = os.path.join(APP_DIR, "fonts", "DejaVuSans.ttf")
FONT_BOLD = os.path.join(APP_DIR, "fonts", "DejaVuSans-Bold.ttf")

_TR_ASCII_MAP = str.maketrans({
    "ğ": "g", "Ğ": "G", "ş": "s", "Ş": "S", "ı": "i", "İ": "I",
    "ö": "o", "Ö": "O", "ü": "u", "Ü": "U", "ç": "c", "Ç": "C",
})


def _make_pdf_object():
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(18, 16, 18)

    use_unicode = os.path.isfile(FONT_REGULAR) and os.path.isfile(FONT_BOLD)
    if use_unicode:
        pdf.add_font("Metin", "", FONT_REGULAR)
        pdf.add_font("Metin", "B", FONT_BOLD)
    return pdf, use_unicode


def _prep_text(text, use_unicode):
    if not text:
        return ""
    return text if use_unicode else text.translate(_TR_ASCII_MAP)


def _write_decision_page(pdf, use_unicode, esas_no, karar_no, tarih, daire, text):
    font_family = "Metin" if use_unicode else "Helvetica"

    pdf.add_page()
    pdf.set_x(pdf.l_margin)
    pdf.set_font(font_family, "B", 13)
    pdf.multi_cell(0, 7, _prep_text(daire or DAIRE_ADI, use_unicode))
    pdf.ln(1)

    pdf.set_x(pdf.l_margin)
    pdf.set_font(font_family, "", 11)
    meta_line = "Esas No: {}    Karar No: {}    Karar Tarihi: {}".format(
        esas_no or "-", karar_no or "-", tarih or "-"
    )
    pdf.multi_cell(0, 6, _prep_text(meta_line, use_unicode))
    pdf.ln(3)

    pdf.set_x(pdf.l_margin)
    pdf.set_draw_color(180, 160, 160)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(5)

    pdf.set_x(pdf.l_margin)
    pdf.set_font(font_family, "", 10.5)
    body = text.strip() if text and text.strip() else "(Bu karar için tam metin alınamadı.)"
    pdf.multi_cell(0, 5.6, _prep_text(body, use_unicode))


def build_combined_pdf(entries):
    pdf, use_unicode = _make_pdf_object()
    for entry in entries:
        _write_decision_page(
            pdf,
            use_unicode,
            entry.get("esasNo", ""),
            entry.get("kararNo", ""),
            entry.get("tarih", ""),
            entry.get("daire", DAIRE_ADI),
            entry.get("text", ""),
        )
    return bytes(pdf.output())


def build_pdf_via_csv(entries):
    csv_bytes = entries_to_csv_bytes(entries)
    entries_from_csv = csv_bytes_to_entries(csv_bytes)
    pdf_bytes = build_combined_pdf(entries_from_csv)
    return pdf_bytes, csv_bytes


def safe_filename(name, fallback="karar"):
    name = (name or fallback).strip()
    name = re.sub(r'[\\/:*?"<>|]', "-", name)
    name = re.sub(r"\s+", "_", name)
    return name[:120] or fallback


# ----------------------------------------------------------------------------
# API uçları
# ----------------------------------------------------------------------------

@app.route("/api/search", methods=["POST"])
def api_search():
    body = request.get_json(force=True, silent=True) or {}
    keyword = (body.get("keyword") or "").strip()
    or_keyword = (body.get("orKeyword") or "").strip()
    page_number = max(1, int(body.get("page", 1)))
    page_size = int(body.get("pageSize", PAGE_SIZE))
    force_refresh = bool(body.get("forceRefresh"))

    filters = {
        "esasYil": body.get("esasYil", ""),
        "esasIlkSiraNo": body.get("esasIlkSiraNo", ""),
        "esasSonSiraNo": body.get("esasSonSiraNo", ""),
        "kararYil": body.get("kararYil", ""),
        "kararIlkSiraNo": body.get("kararIlkSiraNo", ""),
        "kararSonSiraNo": body.get("kararSonSiraNo", ""),
        "baslangicTarihi": body.get("baslangicTarihi", ""),
        "bitisTarihi": body.get("bitisTarihi", ""),
    }

    if not keyword and not or_keyword:
        return jsonify({"success": False, "error": "Lütfen bir arama kelimesi girin."}), 400

    cache_key = make_search_cache_key(keyword, or_keyword, page_number, page_size, filters)

    if not force_refresh:
        cached_payload, cached_at = get_cached_search(cache_key)
        if cached_payload is not None:
            cached_payload["fromCache"] = True
            cached_payload["cachedAt"] = cached_at
            return jsonify(cached_payload)

    try:
        if or_keyword:
            results_a, total_a = ([], None)
            results_b, total_b = ([], None)
            if keyword:
                results_a, total_a = do_single_search(keyword, page_number, page_size, filters)
            if or_keyword:
                results_b, total_b = do_single_search(or_keyword, page_number, page_size, filters)

            results = dedupe_results([results_a, results_b])
            total = None
            total_pages = None
        else:
            results, total = do_single_search(keyword, page_number, page_size, filters)
            total_pages = math.ceil(total / page_size) if total else None

    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 502
    except requests.RequestException as e:
        logger.exception("Arama isteği başarısız")
        return jsonify({"success": False, "error": "Yargıtay sunucusuna ulaşılamadı: {}".format(e)}), 502

    response_payload = {
        "success": True,
        "total": total,
        "count": len(results),
        "page": page_number,
        "pageSize": page_size,
        "totalPages": total_pages,
        "orMode": bool(or_keyword),
        "results": results,
        "fromCache": False,
    }

    save_cached_search(cache_key, response_payload)
    return jsonify(response_payload)


@app.route("/api/decision/<path:doc_id>", methods=["GET"])
def api_decision(doc_id):
    force_refresh = request.args.get("refresh") == "1"

    if not force_refresh:
        cached = get_cached_decision(doc_id)
        if cached is not None:
            return jsonify({
                "success": True,
                "id": doc_id,
                "html": cached["html"],
                "text": cached["text"],
                "textMarked": cached["textMarked"],
                "fromCache": True,
                "cachedAt": cached["createdAt"],
            })

    session = get_session()
    time.sleep(0.3)

    max_retries = 3
    resp = None

    for attempt in range(max_retries):
        try:
            resp = session.get(DOCUMENT_URL, params={"id": doc_id}, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                break

            logger.warning(
                f"Belge {doc_id} için istek başarısız (Deneme {attempt + 1}/{max_retries}). "
                f"Durum Kodu: {resp.status_code if resp else 'Cevap Yok'}"
            )
            time.sleep(1.5 * (attempt + 1))

        except requests.exceptions.Timeout:
            logger.warning(f"Belge {doc_id} için zaman aşımı (Deneme {attempt + 1}/{max_retries})")
            time.sleep(1.5 * (attempt + 1))
        except requests.RequestException as e:
            logger.exception("Karar metni isteği başarısız")
            return jsonify({"success": False, "error": "Karar metni alınamadı: {}".format(e)}), 502

    if DEBUG:
        logger.debug("Karar metni HTTP durumu (%s): %s", doc_id, resp.status_code if resp else "Yok")

    if not resp or resp.status_code != 200:
        return jsonify({
            "success": False,
            "error": "Sunucu {} döndürdü (zaman aşımı olmuş olabilir).".format(
                resp.status_code if resp else "yanıt vermedi"
            ),
        }), 502

    content_type = resp.headers.get("Content-Type", "")
    html_fragment = None

    if "application/json" in content_type:
        try:
            data = resp.json()
            html_fragment = (
                data.get("data")
                if isinstance(data, dict) and isinstance(data.get("data"), str)
                else json.dumps(data, ensure_ascii=False)
            )
        except ValueError:
            html_fragment = resp.text
    else:
        html_fragment = resp.text

    plain_text = html_to_text(html_fragment) if html_fragment else ""
    marked_html = isaretle_madde_referanslari(plain_text).replace("\n", "<br>")

    save_cached_decision(doc_id, html_fragment, plain_text, marked_html)

    return jsonify({
        "success": True,
        "id": doc_id,
        "html": html_fragment,
        "text": plain_text,
        "textMarked": marked_html,
        "fromCache": False,
    })


@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    try:
        clear_cache()
    except Exception as e:
        logger.exception("Önbellek temizleme başarısız")
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True})


@app.route("/api/favorites", methods=["GET"])
def api_favorites_list():
    try:
        favorites = list_favorites()
    except Exception as e:
        logger.exception("Favori listesi alınamadı")
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "favorites": favorites})


@app.route("/api/favorites/ids", methods=["GET"])
def api_favorites_ids():
    try:
        ids = list_favorite_keys()
    except Exception as e:
        logger.exception("Favori id listesi alınamadı")
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "ids": ids})


@app.route("/api/favorites/add", methods=["POST"])
def api_favorites_add():
    body = request.get_json(force=True, silent=True) or {}
    fav_key = (body.get("favKey") or "").strip()
    if not fav_key:
        return jsonify({"success": False, "error": "Geçersiz kayıt (favKey eksik)."}), 400
    try:
        add_favorite(
            fav_key,
            body.get("esasNo", ""),
            body.get("kararNo", ""),
            body.get("tarih", ""),
            body.get("daire", DAIRE_ADI),
            body.get("text", ""),
        )
    except Exception as e:
        logger.exception("Favori eklenemedi")
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True})


@app.route("/api/favorites/remove", methods=["POST"])
def api_favorites_remove():
    body = request.get_json(force=True, silent=True) or {}
    fav_key = (body.get("favKey") or "").strip()
    if not fav_key:
        return jsonify({"success": False, "error": "Geçersiz kayıt (favKey eksik)."}), 400
    try:
        remove_favorite(fav_key)
    except Exception as e:
        logger.exception("Favori kaldırılamadı")
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True})


@app.route("/api/csv", methods=["POST"])
def api_csv():
    body = request.get_json(force=True, silent=True) or {}
    entries = body.get("entries") or []
    keyword = body.get("keyword", "sonuclar")
    page = body.get("page")

    if not entries:
        return jsonify({"success": False, "error": "İndirilecek karar bulunamadı."}), 400

    try:
        csv_bytes = entries_to_csv_bytes(entries)
    except Exception as e:
        logger.exception("CSV üretimi başarısız")
        return jsonify({"success": False, "error": "CSV üretilemedi: {}".format(e)}), 500

    suffix = "_sayfa{}".format(page) if page else ""
    filename = safe_filename("yargitay_11hd_" + keyword + suffix) + ".csv"

    return send_file(
        BytesIO(csv_bytes),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/pdf", methods=["POST"])
def api_pdf_single():
    body = request.get_json(force=True, silent=True) or {}
    esas_no = body.get("esasNo", "")
    karar_no = body.get("kararNo", "")
    tarih = body.get("tarih", "")
    daire = body.get("daire", DAIRE_ADI)
    text = body.get("text", "")

    entry = {
        "esasNo": esas_no,
        "kararNo": karar_no,
        "tarih": tarih,
        "daire": daire,
        "text": text,
    }

    try:
        pdf_bytes, _csv_bytes = build_pdf_via_csv([entry])
    except Exception as e:
        logger.exception("PDF üretimi başarısız (tekli)")
        return jsonify({"success": False, "error": "PDF üretilemedi: {}".format(e)}), 500

    parts = []
    if esas_no:
        parts.append("Esas-" + str(esas_no))
    if karar_no:
        parts.append("Karar-" + str(karar_no))
    filename = safe_filename("_".join(parts) or "karar") + ".pdf"

    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/pdf/all", methods=["POST"])
def api_pdf_all():
    body = request.get_json(force=True, silent=True) or {}
    entries = body.get("entries") or []
    keyword = body.get("keyword", "sonuclar")
    page = body.get("page")

    if not entries:
        return jsonify({"success": False, "error": "İndirilecek karar bulunamadı."}), 400

    try:
        pdf_bytes, _csv_bytes = build_pdf_via_csv(entries)
    except Exception as e:
        logger.exception("PDF üretimi başarısız (toplu)")
        return jsonify({"success": False, "error": "PDF üretilemedi: {}".format(e)}), 500

    suffix = "_sayfa{}".format(page) if page else ""
    filename = safe_filename("yargitay_11hd_" + keyword + suffix) + ".pdf"

    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


# ----------------------------------------------------------------------------
# HTML Şablonları
# ----------------------------------------------------------------------------

INDEX_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<title>Yargıtay 11. Hukuk Dairesi Karar Arama</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  :root {
    --accent: #7a1f2b;
    --accent-dark: #5c1620;
    --bg: #f6f4f1;
    --card-bg: #ffffff;
    --text: #262220;
    --muted: #6b6560;
    --border: #e4ddd6;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Georgia", "Iowan Old Style", serif;
    background: var(--bg);
    color: var(--text);
  }
  header {
    background: var(--accent);
    color: #fff;
    padding: 28px 24px;
  }
  header h1 {
    margin: 0 0 4px 0;
    font-size: 22px;
    font-weight: 600;
  }
  header p {
    margin: 0;
    font-size: 13px;
    opacity: 0.85;
    font-family: Arial, sans-serif;
  }
  main {
    max-width: 880px;
    margin: 0 auto;
    padding: 24px;
  }
  .search-box {
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
  }
  input[type=text], input[type=number], input[type=date] {
    padding: 12px 14px;
    font-size: 15px;
    border: 1px solid var(--border);
    border-radius: 6px;
    font-family: Arial, sans-serif;
  }
  input[type=text] { flex: 1; }
  button {
    background: var(--accent);
    color: #fff;
    border: none;
    padding: 12px 22px;
    border-radius: 6px;
    font-size: 15px;
    cursor: pointer;
    font-family: Arial, sans-serif;
  }
  button:hover { background: var(--accent-dark); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }

  .advanced-toggle {
    background: none;
    color: var(--accent);
    border: none;
    padding: 4px 0;
    font-size: 13px;
    text-decoration: underline;
    cursor: pointer;
    margin-bottom: 14px;
  }
  .advanced-panel {
    display: none;
    background: #fff;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 18px;
    margin-bottom: 16px;
    font-family: Arial, sans-serif;
  }
  .advanced-panel.open { display: block; }
  .advanced-panel h3 {
    margin: 0 0 10px 0;
    font-size: 13px;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .advanced-row {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 12px;
    align-items: flex-end;
  }
  .field-group {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .field-group label {
    font-size: 12px;
    color: var(--muted);
  }
  .field-group input {
    width: 130px;
  }
  .field-group input.wide {
    width: 220px;
  }
  .advanced-hint {
    font-size: 12px;
    color: var(--muted);
    margin-top: 2px;
  }

  .status-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }
  .status-left {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  #status {
    font-family: Arial, sans-serif;
    font-size: 13px;
    color: var(--muted);
    min-height: 18px;
  }
  .cache-badge {
    font-family: Arial, sans-serif;
    font-size: 11.5px;
    background: #eef3fb;
    color: #2c4a76;
    border: 1px solid #c7d8ef;
    padding: 2px 8px;
    border-radius: 10px;
    white-space: nowrap;
  }
  .btn-secondary {
    background: #fff;
    color: var(--accent);
    border: 1px solid var(--accent);
    padding: 8px 14px;
    font-size: 13px;
    border-radius: 6px;
    white-space: nowrap;
  }
  .btn-secondary:hover { background: #fbeaea; }
  .btn-small {
    background: #fff;
    color: var(--accent);
    border: 1px solid var(--accent);
    padding: 5px 12px;
    font-size: 12.5px;
    border-radius: 5px;
  }
  .btn-small:hover { background: #fbeaea; }
  .btn-refresh {
    display: none;
  }
  .star-btn {
    background: #fff;
    border: 1px solid var(--border);
    color: #c9a227;
    padding: 5px 10px;
    font-size: 15px;
    border-radius: 5px;
    line-height: 1;
    cursor: pointer;
  }
  .star-btn:hover:not(:disabled) { background: #fff8e6; border-color: #c9a227; }
  .star-btn.favorited { background: #fff3cf; border-color: #c9a227; }
  .star-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 18px;
    margin-bottom: 14px;
  }
  .card.hidden-by-filter { display: none; }
  .meta-line {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 14px;
    font-size: 13px;
  }
  .meta-line b { color: var(--accent); }
  .meta-line .spacer { flex: 1; }
  .full-text {
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px dashed var(--border);
    white-space: pre-wrap;
    font-size: 14.5px;
    line-height: 1.6;
    max-height: 480px;
    overflow-y: auto;
  }
  .full-text mark.madde-ref {
    background: #f3d9a8;
    color: #4a3200;
    padding: 0 2px;
    border-radius: 3px;
    font-weight: bold;
  }
  .loading-text {
    font-family: Arial, sans-serif;
    font-size: 13px;
    color: var(--muted);
    margin-top: 12px;
  }
  .empty {
    font-family: Arial, sans-serif;
    color: var(--muted);
    text-align: center;
    margin-top: 40px;
  }
  .error-box {
    font-family: Arial, sans-serif;
    background: #fdecec;
    border: 1px solid #f3b8b8;
    color: #8a1f1f;
    padding: 12px 14px;
    border-radius: 6px;
    margin-bottom: 16px;
    font-size: 13px;
  }
  .info-box {
    font-family: Arial, sans-serif;
    background: #eef3fb;
    border: 1px solid #c7d8ef;
    color: #2c4a76;
    padding: 10px 14px;
    border-radius: 6px;
    margin-bottom: 16px;
    font-size: 12.5px;
  }
  .pagination {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
    gap: 6px;
    margin: 24px 0 8px 0;
    font-family: Arial, sans-serif;
  }
  .page-btn {
    background: #fff;
    color: var(--text);
    border: 1px solid var(--border);
    padding: 6px 12px;
    font-size: 13px;
    border-radius: 5px;
    min-width: 36px;
  }
  .page-btn:hover:not(:disabled) { background: #fbeaea; border-color: var(--accent); }
  .page-btn.active {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent);
    font-weight: bold;
  }
  .page-btn:disabled { opacity: 0.35; cursor: not-allowed; }
  .page-dots {
    padding: 0 4px;
    color: var(--muted);
    font-size: 13px;
  }
</style>
</head>
<body>
<header>
  <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap;">
    <div>
      <h1>Yargıtay 11. Hukuk Dairesi &mdash; Karar Arama</h1>
      <p>karararama.yargitay.gov.tr üzerinde 11. Hukuk Dairesi kararları için anahtar kelime araması</p>
    </div>
    <a href="/favoriler" style="font-family:Arial,sans-serif; font-size:13px; color:#fff; background:rgba(255,255,255,0.15); padding:8px 14px; border-radius:6px; text-decoration:none; white-space:nowrap;">★ Favorilerim</a>
  </div>
</header>
<main>
  <div class="search-box">
    <input type="text" id="keyword" placeholder="Örn: marka hakkına tecavüz, haksız rekabet..." />
    <button id="searchBtn" onclick="doSearch(1, false)">Ara</button>
  </div>
  <button type="button" class="advanced-toggle" onclick="toggleAdvanced()">+ Gelişmiş Arama (Esas/Karar No, Tarih, VEYA / DEĞİL)</button>

  <div class="advanced-panel" id="advancedPanel">
    <h3>Esas No</h3>
    <div class="advanced-row">
      <div class="field-group">
        <label>Yıl</label>
        <input type="number" id="esasYil" placeholder="2024">
      </div>
      <div class="field-group">
        <label>Sıra No (başlangıç)</label>
        <input type="number" id="esasIlkSiraNo" placeholder="1">
      </div>
      <div class="field-group">
        <label>Sıra No (bitiş)</label>
        <input type="number" id="esasSonSiraNo" placeholder="9999">
      </div>
    </div>

    <h3>Karar No</h3>
    <div class="advanced-row">
      <div class="field-group">
        <label>Yıl</label>
        <input type="number" id="kararYil" placeholder="2024">
      </div>
      <div class="field-group">
        <label>Sıra No (başlangıç)</label>
        <input type="number" id="kararIlkSiraNo" placeholder="1">
      </div>
      <div class="field-group">
        <label>Sıra No (bitiş)</label>
        <input type="number" id="kararSonSiraNo" placeholder="9999">
      </div>
    </div>

    <h3>Tarih Aralığı</h3>
    <div class="advanced-row">
      <div class="field-group">
        <label>Başlangıç Tarihi</label>
        <input type="date" id="baslangicTarihi">
      </div>
      <div class="field-group">
        <label>Bitiş Tarihi</label>
        <input type="date" id="bitisTarihi">
      </div>
    </div>

    <h3>Çoklu Kelime</h3>
    <div class="advanced-row">
      <div class="field-group">
        <label>VEYA — bu kelimeyi de içeren sonuçları ekle</label>
        <input type="text" id="orKeyword" class="wide" placeholder="Örn: marka">
      </div>
      <div class="field-group">
        <label>DEĞİL — bu kelimeyi içerenleri gizle</label>
        <input type="text" id="notKeyword" class="wide" placeholder="Örn: patent">
      </div>
    </div>
    <div class="advanced-hint">
      VE (AND) için ana arama kutusuna birden fazla kelime yazmanız yeterli (örn. "haksız rekabet marka").
      VEYA aktifken sayfalama sınırlıdır; DEĞİL filtresi, kararın tam metni yüklendikten sonra istemci
      tarafında uygulanır.
    </div>
  </div>

  <div class="status-row">
    <div class="status-left">
      <div id="status"></div>
      <span id="cacheBadge" class="cache-badge" style="display:none;"></span>
    </div>
    <div style="display:flex; gap:8px; flex-wrap:wrap;">
      <button id="refreshBtn" class="btn-secondary btn-refresh" onclick="refreshSearch()">🔄 Yenile</button>
      <button id="downloadAllBtn" class="btn-secondary" onclick="downloadAll()" style="display:none;">
        Bu Sayfadaki Sonuçları İndir (PDF)
      </button>
      <button id="downloadAllCsvBtn" class="btn-secondary" onclick="downloadAllCsv()" style="display:none;">
        Bu Sayfayı CSV Olarak İndir
      </button>
      <button id="clearCacheBtn" class="btn-secondary" onclick="clearAllCache()" title="Yerel önbelleği tamamen temizler">
        🗑️ Önbelleği Temizle
      </button>
    </div>
  </div>
  <div id="results"></div>
  <div id="pagination" class="pagination"></div>
</main>

<script>
let currentResults = {};
let currentKeyword = '';
let currentPage = 1;
let totalPages = 1;
let notKeywordActive = '';
let lastForceRefresh = false;
let favoriteKeysSet = new Set();

function toggleAdvanced() {
  document.getElementById('advancedPanel').classList.toggle('open');
}

function collectFilters() {
  return {
    esasYil: document.getElementById('esasYil').value.trim(),
    esasIlkSiraNo: document.getElementById('esasIlkSiraNo').value.trim(),
    esasSonSiraNo: document.getElementById('esasSonSiraNo').value.trim(),
    kararYil: document.getElementById('kararYil').value.trim(),
    kararIlkSiraNo: document.getElementById('kararIlkSiraNo').value.trim(),
    kararSonSiraNo: document.getElementById('kararSonSiraNo').value.trim(),
    baslangicTarihi: document.getElementById('baslangicTarihi').value.trim(),
    bitisTarihi: document.getElementById('bitisTarihi').value.trim(),
    orKeyword: document.getElementById('orKeyword').value.trim(),
  };
}

function refreshSearch() {
  doSearch(currentPage, true);
}

async function clearAllCache() {
  const clearBtn = document.getElementById('clearCacheBtn');
  clearBtn.disabled = true;
  try {
    await fetch('/api/cache/clear', { method: 'POST' });
    alert('Önbellek temizlendi. Bir sonraki arama sunucudan tekrar çekilecek.');
  } catch (err) {
    alert('Önbellek temizlenirken hata oluştu: ' + err.message);
  } finally {
    clearBtn.disabled = false;
  }
}

async function doSearch(page, forceRefresh) {
  const keywordInput = document.getElementById('keyword').value.trim();
  const statusEl = document.getElementById('status');
  const cacheBadge = document.getElementById('cacheBadge');
  const resultsEl = document.getElementById('results');
  const paginationEl = document.getElementById('pagination');
  const btn = document.getElementById('searchBtn');
  const refreshBtn = document.getElementById('refreshBtn');
  const downloadAllBtn = document.getElementById('downloadAllBtn');
  const downloadAllCsvBtn = document.getElementById('downloadAllCsvBtn');

  const filters = collectFilters();
  notKeywordActive = document.getElementById('notKeyword').value.trim().toLocaleLowerCase('tr-TR');

  const keyword = keywordInput || currentKeyword;
  const hasStructuredFilter = filters.esasYil || filters.kararYil || filters.baslangicTarihi || filters.bitisTarihi;

  if (!keyword && !filters.orKeyword && !hasStructuredFilter) {
    statusEl.textContent = 'Lütfen bir arama kelimesi girin veya gelişmiş filtre kullanın.';
    return;
  }
  currentKeyword = keyword;
  currentPage = page || 1;
  lastForceRefresh = !!forceRefresh;

  currentResults = {};
  downloadAllBtn.style.display = 'none';
  downloadAllCsvBtn.style.display = 'none';
  cacheBadge.style.display = 'none';

  btn.disabled = true;
  refreshBtn.disabled = true;
  resultsEl.innerHTML = '';
  paginationEl.innerHTML = '';
  statusEl.textContent = forceRefresh ? 'Sunucudan yeniden çekiliyor...' : 'Aranıyor...';
  window.scrollTo({ top: 0, behavior: 'smooth' });

  try {
   const res = await fetch('/api/search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign(
        {keyword: keyword, page: currentPage, forceRefresh: !!forceRefresh},
        filters
      ))
    });

    const contentType = res.headers.get("content-type");
    if (!res.ok || !contentType || !contentType.includes("application/json")) {
      const rawText = await res.text();
      console.error("Sunucudan dönen ham yanıt:", rawText);

      statusEl.textContent = '';
      resultsEl.innerHTML = `<div class="error-box">
        Sunucudan geçersiz yanıt alındı (Status: ${res.status}). 
        Yargıtay servisi yanıt vermiyor veya zaman aşımına uğramış olabilir.
      </div>`;
      btn.disabled = false;
      refreshBtn.disabled = false;
      return;
    }


    // 2. Artık güvenle JSON parse edebiliriz
    const data = await res.json();

    if (!data.success) {
      statusEl.textContent = '';
      resultsEl.innerHTML = '<div class="error-box">' + (data.error || 'Bilinmeyen hata.') + '</div>';
      btn.disabled = false;
      refreshBtn.disabled = false;
      refreshBtn.disabled = false;
      return;
    }

    try {
      const favRes = await fetch('/api/favorites/ids');
      const favData = await favRes.json();
      favoriteKeysSet = new Set(favData.ids || []);
    } catch (e) {
      favoriteKeysSet = new Set();
    }

    if (!data.results || data.results.length === 0) {
      statusEl.textContent = '';
      resultsEl.innerHTML = '<div class="empty">Sonuç bulunamadı.</div>';
      btn.disabled = false;
      refreshBtn.style.display = 'inline-block';
      return;
    }

    totalPages = data.totalPages || 1;

    let statusText = 'Sayfa ' + data.page + ' / ' + totalPages + ' — ' + data.count + ' sonuç' +
      (data.total ? ' (toplam ' + data.total + ')' : '');
    statusEl.textContent = statusText;

    if (data.fromCache) {
      let cachedWhen = '';
      try {
        const d = new Date(data.cachedAt);
        cachedWhen = d.toLocaleString('tr-TR');
      } catch (e) {}
      cacheBadge.textContent = '📦 Önbellekten' + (cachedWhen ? ' (' + cachedWhen + ')' : '');
      cacheBadge.style.display = 'inline-block';
    }

    refreshBtn.style.display = 'inline-block';

    resultsEl.innerHTML = '';
    if (data.orMode) {
      resultsEl.innerHTML = '<div class="info-box">VEYA modu aktif: iki kelimenin sonuçları birleştirildi. Bu modda toplam sonuç sayısı ve tam sayfalama gösterilemez.</div>';
    }

    resultsEl.innerHTML += data.results.map(function(r, idx) { return renderCard(r, idx); }).join('');
    downloadAllBtn.style.display = 'inline-block';
    downloadAllCsvBtn.style.display = 'inline-block';

    data.results.forEach(function(r, idx) {
      const raw = r.raw || {};
      const esasNo = pickField(raw, ['esasNo', 'esas_no', 'esas']) || '';
      const kararNo = pickField(raw, ['kararNo', 'karar_no', 'karar']) || '';
      const favKey = r.id ? String(r.id) : (esasNo + '|' + kararNo);
      currentResults[idx] = {
        esasNo: esasNo,
        kararNo: kararNo,
        tarih: pickField(raw, ['kararTarihi', 'karar_tarihi', 'tarih']) || '',
        daire: pickField(raw, ['hukukBirimiText', 'birimAdi', 'daire']) || '11. Hukuk Dairesi',
        text: null,
        favKey: favKey,
      };
      const starBtn = document.getElementById('starbtn-' + idx);
      if (starBtn && favoriteKeysSet.has(favKey)) {
        starBtn.classList.add('favorited');
        starBtn.textContent = '★';
      }
      if (r.id) {
        setTimeout(function() {
          fetchFullText(r.id, idx, forceRefresh);
        }, idx * 300);
      } else {
        const box = document.getElementById('fulltext-' + idx);
        if (box) box.innerHTML = '<span class="loading-text">Bu kayıt için belge kimliği (id) bulunamadı, tam metin otomatik getirilemedi.</span>';
      }
    });

    if (!data.orMode) {
      renderPagination();
    }

  } catch (err) {
    statusEl.textContent = '';
    resultsEl.innerHTML = '<div class="error-box">İstek sırasında hata oluştu: ' + err + '</div>';
  } finally {
    btn.disabled = false;
    refreshBtn.disabled = false;
  }
}

function renderPagination() {
  const paginationEl = document.getElementById('pagination');
  if (totalPages <= 1) {
    paginationEl.innerHTML = '';
    return;
  }

  const pages = [];
  const windowSize = 2;

  pages.push(1);
  for (let p = currentPage - windowSize; p <= currentPage + windowSize; p++) {
    if (p > 1 && p < totalPages) pages.push(p);
  }
  if (totalPages > 1) pages.push(totalPages);

  const uniquePages = [...new Set(pages)].sort(function(a, b) { return a - b; });

  let html = '';
  html += '<button class="page-btn" ' + (currentPage <= 1 ? 'disabled' : '') + ' onclick="doSearch(' + (currentPage - 1) + ', false)">&laquo; Önceki</button>';

  let prev = 0;
  uniquePages.forEach(function(p) {
    if (prev && p - prev > 1) {
      html += '<span class="page-dots">...</span>';
    }
    html += '<button class="page-btn ' + (p === currentPage ? 'active' : '') + '" onclick="doSearch(' + p + ', false)">' + p + '</button>';
    prev = p;
  });

  html += '<button class="page-btn" ' + (currentPage >= totalPages ? 'disabled' : '') + ' onclick="doSearch(' + (currentPage + 1) + ', false)">Sonraki &raquo;</button>';

  paginationEl.innerHTML = html;
}

function pickField(raw, candidates) {
  for (const key of candidates) {
    if (raw[key] !== undefined && raw[key] !== null && raw[key] !== '') {
      return raw[key];
    }
  }
  return null;
}

function renderCard(result, idx) {
  const raw = result.raw || {};
  const esasNo = pickField(raw, ['esasNo', 'esas_no', 'esas']);
  const kararNo = pickField(raw, ['kararNo', 'karar_no', 'karar']);
  const tarih = pickField(raw, ['kararTarihi', 'karar_tarihi', 'tarih']);
  const daire = pickField(raw, ['hukukBirimiText', 'birimAdi', 'daire']) || '11. Hukuk Dairesi';

  const metaParts = [];
  if (esasNo) metaParts.push('<span><b>Esas No:</b> ' + escapeHtml(esasNo) + '</span>');
  if (kararNo) metaParts.push('<span><b>Karar No:</b> ' + escapeHtml(kararNo) + '</span>');
  if (tarih) metaParts.push('<span><b>Karar Tarihi:</b> ' + escapeHtml(tarih) + '</span>');
  metaParts.push('<span><b>Daire:</b> ' + escapeHtml(daire) + '</span>');
  metaParts.push('<span class="spacer"></span>');
  metaParts.push('<button class="btn-small" id="dlbtn-' + idx + '" onclick="downloadOne(' + idx + ')" disabled>İndir (PDF)</button>');
  metaParts.push('<button class="btn-small" id="csvbtn-' + idx + '" onclick="downloadOneCsv(' + idx + ')" disabled>CSV</button>');
  metaParts.push('<button class="star-btn" id="starbtn-' + idx + '" onclick="toggleFavorite(' + idx + ')" disabled title="Favorilere ekle">☆</button>');

  return '<div class="card" id="card-' + idx + '"><div class="meta-line">' + metaParts.join('') + '</div>' +
    '<div id="fulltext-' + idx + '" class="loading-text">Tam metin getiriliyor...</div></div>';
}

function renderCard(result, idx) {
  const raw = result.raw || {};
  const esasNo = pickField(raw, ['esasNo', 'esas_no', 'esas']);
  const kararNo = pickField(raw, ['kararNo', 'karar_no', 'karar']);
  const tarih = pickField(raw, ['kararTarihi', 'karar_tarihi', 'tarih']);
  const daire = pickField(raw, ['hukukBirimiText', 'birimAdi', 'daire']) || '11. Hukuk Dairesi';

  const metaParts = [];
  if (esasNo) metaParts.push('<span><b>Esas No:</b> ' + escapeHtml(esasNo) + '</span>');
  if (kararNo) metaParts.push('<span><b>Karar No:</b> ' + escapeHtml(kararNo) + '</span>');
  if (tarih) metaParts.push('<span><b>Karar Tarihi:</b> ' + escapeHtml(tarih) + '</span>');
  metaParts.push('<span><b>Daire:</b> ' + escapeHtml(daire) + '</span>');
  metaParts.push('<span class="spacer"></span>');
  metaParts.push('<button class="btn-small" id="dlbtn-' + idx + '" onclick="downloadOne(' + idx + ')" disabled>İndir (PDF)</button>');
  metaParts.push('<button class="btn-small" id="csvbtn-' + idx + '" onclick="downloadOneCsv(' + idx + ')" disabled>CSV</button>');
  metaParts.push('<button class="star-btn" id="starbtn-' + idx + '" onclick="toggleFavorite(' + idx + ')" disabled title="Favorilere ekle">☆</button>');

  return '<div class="card" id="card-' + idx + '"><div class="meta-line">' + metaParts.join('') + '</div>' +
    '<div id="fulltext-' + idx + '" class="loading-text">Tam metin getiriliyor...</div></div>';
}

async function fetchFullText(id, idx, forceRefresh) {
  const box = document.getElementById('fulltext-' + idx);
  try {
    const url = '/api/decision/' + encodeURIComponent(id) + (forceRefresh ? '?refresh=1' : '');
    const res = await fetch(url);
    const data = await res.json();
    if (!data.success) {
      box.innerHTML = '<span class="loading-text">Tam metin alınamadı: ' + (data.error || '') + '</span>';
      return;
    }
    const text = data.text && data.text.trim() ? data.text : '(Bu karar için metin bulunamadı.)';
    box.className = 'full-text';
    box.innerHTML = data.textMarked || text;

    if (currentResults[idx]) {
      currentResults[idx].text = text;
    }
    const dlBtn = document.getElementById('dlbtn-' + idx);
    if (dlBtn) dlBtn.disabled = false;
    const csvBtn = document.getElementById('csvbtn-' + idx);
    if (csvBtn) csvBtn.disabled = false;
    const starBtn = document.getElementById('starbtn-' + idx);
    if (starBtn) starBtn.disabled = false;

    if (notKeywordActive && text.toLocaleLowerCase('tr-TR').includes(notKeywordActive)) {
      const card = document.getElementById('card-' + idx);
      if (card) card.classList.add('hidden-by-filter');
    }
  } catch (err) {
    box.innerHTML = '<span class="loading-text">Tam metin alınırken hata oluştu.</span>';
  }
}

async function toggleFavorite(idx) {
  const entry = currentResults[idx];
  if (!entry || !entry.favKey) return;
  const starBtn = document.getElementById('starbtn-' + idx);
  if (starBtn) starBtn.disabled = true;

  const isFavorited = favoriteKeysSet.has(entry.favKey);

  try {
    if (isFavorited) {
      await fetch('/api/favorites/remove', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ favKey: entry.favKey })
      });
      favoriteKeysSet.delete(entry.favKey);
      if (starBtn) { starBtn.classList.remove('favorited'); starBtn.textContent = '☆'; }
    } else {
      await fetch('/api/favorites/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          favKey: entry.favKey,
          esasNo: entry.esasNo,
          kararNo: entry.kararNo,
          tarih: entry.tarih,
          daire: entry.daire,
          text: entry.text
        })
      });
      favoriteKeysSet.add(entry.favKey);
      if (starBtn) { starBtn.classList.add('favorited'); starBtn.textContent = '★'; }
    }
  } catch (err) {
    alert('Favori işlemi sırasında hata oluştu: ' + err.message);
  } finally {
    if (starBtn) starBtn.disabled = false;
  }
}

async function downloadBlobResponse(res, fallbackName) {
  if (!res.ok) {
    let msg = 'Sunucu hatası.';
    try {
      const errData = await res.json();
      msg = errData.error || msg;
    } catch (e) {}
    throw new Error(msg);
  }
  const blob = await res.blob();
  const disposition = res.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename="?([^"]+)"?/);
  const filename = match ? match[1] : fallbackName;

  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

async function downloadOne(idx) {
  const entry = currentResults[idx];
  if (!entry || !entry.text) return;
  const dlBtn = document.getElementById('dlbtn-' + idx);
  if (dlBtn) { dlBtn.disabled = true; dlBtn.textContent = 'Hazırlanıyor...'; }
  try {
    const res = await fetch('/api/pdf', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(entry)
    });
    await downloadBlobResponse(res, 'karar.pdf');
  } catch (err) {
    alert('PDF oluşturulurken hata oluştu: ' + err.message);
  } finally {
    if (dlBtn) { dlBtn.disabled = false; dlBtn.textContent = 'İndir (PDF)'; }
  }
}

async function downloadOneCsv(idx) {
  const entry = currentResults[idx];
  if (!entry || !entry.text) return;
  const csvBtn = document.getElementById('csvbtn-' + idx);
  if (csvBtn) { csvBtn.disabled = true; csvBtn.textContent = '...'; }
  try {
    const res = await fetch('/api/csv', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ keyword: (entry.esasNo || 'karar'), entries: [entry] })
    });
    await downloadBlobResponse(res, 'karar.csv');
  } catch (err) {
    alert('CSV oluşturulurken hata oluştu: ' + err.message);
  } finally {
    if (csvBtn) { csvBtn.disabled = false; csvBtn.textContent = 'CSV'; }
  }
}

async function downloadAll() {
  const keys = Object.keys(currentResults);
  if (keys.length === 0) return;

  const btn = document.getElementById('downloadAllBtn');
  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = 'Hazırlanıyor...';

  const entries = keys.map(function(idx) { return currentResults[idx]; });

  try {
    const res = await fetch('/api/pdf/all', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ keyword: currentKeyword, page: currentPage, entries: entries })
    });
    await downloadBlobResponse(res, 'yargitay_sonuclar.pdf');
  } catch (err) {
    alert('PDF oluşturulurken hata oluştu: ' + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
}

async function downloadAllCsv() {
  const keys = Object.keys(currentResults);
  if (keys.length === 0) return;

  const btn = document.getElementById('downloadAllCsvBtn');
  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = 'Hazırlanıyor...';

  const entries = keys.map(function(idx) { return currentResults[idx]; });

  try {
    const res = await fetch('/api/csv', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ keyword: currentKeyword, page: currentPage, entries: entries })
    });
    await downloadBlobResponse(res, 'yargitay_sonuclar.csv');
  } catch (err) {
    alert('CSV oluşturulurken hata oluştu: ' + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
}

document.getElementById('keyword').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') doSearch(1, false);
});
</script>
</body>
</html>
"""


FAVORITES_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<title>Favori Kararlarım — Yargıtay 11. Hukuk Dairesi</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  :root {
    --accent: #7a1f2b;
    --accent-dark: #5c1620;
    --bg: #f6f4f1;
    --card-bg: #ffffff;
    --text: #262220;
    --muted: #6b6560;
    --border: #e4ddd6;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Georgia", "Iowan Old Style", serif;
    background: var(--bg);
    color: var(--text);
  }
  header {
    background: var(--accent);
    color: #fff;
    padding: 28px 24px;
  }
  header h1 { margin: 0 0 4px 0; font-size: 22px; font-weight: 600; }
  header a.back-link {
    font-family: Arial, sans-serif;
    font-size: 13px;
    color: #fff;
    background: rgba(255,255,255,0.15);
    padding: 8px 14px;
    border-radius: 6px;
    text-decoration: none;
    white-space: nowrap;
  }
  main { max-width: 880px; margin: 0 auto; padding: 24px; }
  .status-row {
    display: flex; align-items: center; justify-content: space-between;
    gap: 12px; margin-bottom: 16px; flex-wrap: wrap;
    font-family: Arial, sans-serif;
  }
  #status { font-size: 13px; color: var(--muted); }
  .btn-secondary {
    background: #fff; color: var(--accent); border: 1px solid var(--accent);
    padding: 8px 14px; font-size: 13px; border-radius: 6px; cursor: pointer;
    font-family: Arial, sans-serif; white-space: nowrap;
  }
  .btn-secondary:hover { background: #fbeaea; }
  .btn-secondary:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-small {
    background: #fff; color: var(--accent); border: 1px solid var(--accent);
    padding: 5px 12px; font-size: 12.5px; border-radius: 5px; cursor: pointer;
  }
  .btn-small:hover { background: #fbeaea; }
  .btn-remove {
    background: #fff; color: #8a1f1f; border: 1px solid #e3b3b3;
    padding: 5px 12px; font-size: 12.5px; border-radius: 5px; cursor: pointer;
  }
  .btn-remove:hover { background: #fdecec; }
  .card {
    background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 16px 18px; margin-bottom: 14px;
  }
  .meta-line { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; font-size: 13px; font-family: Arial, sans-serif; }
  .meta-line b { color: var(--accent); }
  .meta-line .spacer { flex: 1; }
  .full-text {
    margin-top: 14px; padding-top: 12px; border-top: 1px dashed var(--border);
    white-space: pre-wrap; font-size: 14.5px; line-height: 1.6;
    max-height: 480px; overflow-y: auto;
  }
  .empty {
    font-family: Arial, sans-serif; color: var(--muted); text-align: center; margin-top: 40px;
  }
  .error-box {
    font-family: Arial, sans-serif; background: #fdecec; border: 1px solid #f3b8b8;
    color: #8a1f1f; padding: 12px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 13px;
  }
</style>
</head>
<body>
<header>
  <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap;">
    <h1>★ Favori Kararlarım</h1>
    <a href="/" class="back-link">&laquo; Aramaya Dön</a>
  </div>
</header>
<main>
  <div class="status-row">
    <div id="status">Yükleniyor...</div>
    <div style="display:flex; gap:8px; flex-wrap:wrap;">
      <button id="downloadAllBtn" class="btn-secondary" onclick="downloadAll()" style="display:none;">Tümünü PDF İndir</button>
      <button id="downloadAllCsvBtn" class="btn-secondary" onclick="downloadAllCsv()" style="display:none;">Tümünü CSV İndir</button>
    </div>
  </div>
  <div id="results"></div>
</main>

<script>
let favorites = [];

async function loadFavorites() {
  const statusEl = document.getElementById('status');
  const resultsEl = document.getElementById('results');
  const downloadAllBtn = document.getElementById('downloadAllBtn');
  const downloadAllCsvBtn = document.getElementById('downloadAllCsvBtn');

  try {
    const res = await fetch('/api/favorites');
    const data = await res.json();
    if (!data.success) {
      statusEl.textContent = '';
      resultsEl.innerHTML = '<div class="error-box">' + (data.error || 'Favoriler alınamadı.') + '</div>';
      return;
    }
    favorites = data.favorites || [];
    if (favorites.length === 0) {
      statusEl.textContent = '';
      resultsEl.innerHTML = '<div class="empty">Henüz favori eklemediniz. Arama sonuçlarındaki ☆ butonuna tıklayarak karar ekleyebilirsiniz.</div>';
      return;
    }
    statusEl.textContent = favorites.length + ' favori karar';
    downloadAllBtn.style.display = 'inline-block';
    downloadAllCsvBtn.style.display = 'inline-block';
    resultsEl.innerHTML = favorites.map(function(f, idx) { return renderCard(f, idx); }).join('');
  } catch (err) {
    statusEl.textContent = '';
    resultsEl.innerHTML = '<div class="error-box">Favoriler yüklenirken hata oluştu: ' + err + '</div>';
  }
}

function renderCard(f, idx) {
  const metaParts = [];
  if (f.esasNo) metaParts.push('<span><b>Esas No:</b> ' + f.esasNo + '</span>');
  if (f.kararNo) metaParts.push('<span><b>Karar No:</b> ' + f.kararNo + '</span>');
  if (f.tarih) metaParts.push('<span><b>Karar Tarihi:</b> ' + f.tarih + '</span>');
  metaParts.push('<span><b>Daire:</b> ' + (f.daire || '11. Hukuk Dairesi') + '</span>');
  metaParts.push('<span class="spacer"></span>');
  metaParts.push('<button class="btn-small" onclick="downloadOne(' + idx + ')">İndir (PDF)</button>');
  metaParts.push('<button class="btn-small" onclick="downloadOneCsv(' + idx + ')">CSV</button>');
  metaParts.push('<button class="btn-remove" onclick="removeFavorite(' + JSON.stringify(f.favKey) + ', ' + idx + ')">Kaldır</button>');

  const text = f.text && f.text.trim() ? f.text : '(Bu karar için metin kaydedilmemiş.)';

  return '<div class="card" id="favcard-' + idx + '"><div class="meta-line">' + metaParts.join('') + '</div>' +
    '<div class="full-text">' + text.replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</div></div>';
}

async function downloadBlobResponse(res, fallbackName) {
  if (!res.ok) {
    let msg = 'Sunucu hatası.';
    try { const errData = await res.json(); msg = errData.error || msg; } catch (e) {}
    throw new Error(msg);
  }
  const blob = await res.blob();
  const disposition = res.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename="?([^"]+)"?/);
  const filename = match ? match[1] : fallbackName;
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

async function downloadOne(idx) {
  const f = favorites[idx];
  if (!f) return;
  try {
    const res = await fetch('/api/pdf', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ esasNo: f.esasNo, kararNo: f.kararNo, tarih: f.tarih, daire: f.daire, text: f.text })
    });
    await downloadBlobResponse(res, 'karar.pdf');
  } catch (err) {
    alert('PDF oluşturulurken hata oluştu: ' + err.message);
  }
}

async function downloadOneCsv(idx) {
  const f = favorites[idx];
  if (!f) return;
  try {
    const res = await fetch('/api/csv', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ keyword: (f.esasNo || 'karar'), entries: [f] })
    });
    await downloadBlobResponse(res, 'karar.csv');
  } catch (err) {
    alert('CSV oluşturulurken hata oluştu: ' + err.message);
  }
}

async function downloadAll() {
  if (favorites.length === 0) return;
  const btn = document.getElementById('downloadAllBtn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/pdf/all', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ keyword: 'favoriler', entries: favorites })
    });
    await downloadBlobResponse(res, 'favori_kararlar.pdf');
  } catch (err) {
    alert('PDF oluşturulurken hata oluştu: ' + err.message);
  } finally {
    btn.disabled = false;
  }
}

async function downloadAllCsv() {
  if (favorites.length === 0) return;
  const btn = document.getElementById('downloadAllCsvBtn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/csv', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ keyword: 'favoriler', entries: favorites })
    });
    await downloadBlobResponse(res, 'favori_kararlar.csv');
  } catch (err) {
    alert('CSV oluşturulurken hata oluştu: ' + err.message);
  } finally {
    btn.disabled = false;
  }
}

async function removeFavorite(favKey, idx) {
  try {
    await fetch('/api/favorites/remove', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ favKey: favKey })
    });
    const card = document.getElementById('favcard-' + idx);
    if (card) card.remove();
    favorites[idx] = null;
    const remaining = favorites.filter(function(f) { return f; });
    document.getElementById('status').textContent = remaining.length + ' favori karar';
    if (remaining.length === 0) {
      document.getElementById('results').innerHTML = '<div class="empty">Henüz favori eklemediniz. Arama sonuçlarındaki ☆ butonuna tıklayarak karar ekleyebilirsiniz.</div>';
      document.getElementById('downloadAllBtn').style.display = 'none';
      document.getElementById('downloadAllCsvBtn').style.display = 'none';
    }
  } catch (err) {
    alert('Favori kaldırılırken hata oluştu: ' + err.message);
  }
}

loadFavorites();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# Uygulama Başlatma
# ----------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/favoriler")
def favorites_page():
    return render_template_string(FAVORITES_HTML)


def open_browser_when_ready():
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    threading.Timer(1.0, open_browser_when_ready).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
