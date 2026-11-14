"""Список ЖК Воронежа и области: сбор из открытых источников, слияние, оценка (spec-leads §3.0a).

Источники:
- реестр новостроек наш.дом.рф (ЕИСЖС) — все дома, строившиеся по проектным декларациям:
  название ЖК, число квартир, срок сдачи, координаты, застройщик;
- выгрузка категории «Жилой комплекс» из Яндекс Карт (`yandex_maps.json`, собирается вручную из
  браузера: у Карт защита от автоматических запросов, обходить её нельзя) — ЖК, которых нет в
  реестре (старые дома), число оценок, сайт и ссылки на соцсети.

Результат — `complexes/voronezh.csv` в репозитории данных: одна строка — один ЖК, сверху те,
что подходят лучше всего. Разведка берёт из него названия для поиска и готовые ссылки на чаты.

Запуск на сервере: `docker compose exec bot python -m app.leads.complexes`.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import io
import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.leads.text import normalize, normalize_link

log = logging.getLogger(__name__)

CSV_PATH = "complexes/voronezh.csv"
YANDEX_PATH = "complexes/yandex_maps.json"
DOMRF_API = "https://xn--80az8a.xn--d1aqf.xn--p1ai/%D1%81%D0%B5%D1%80%D0%B2%D0%B8%D1%81%D1%8B/api/kn/object"
REGION = 36  # Воронежская область
CENTER = (51.672, 39.184)  # Воронеж, площадь Ленина
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

NAME, SCORE, GRADE, STAGE, READY, FLATS, HOUSES, PLACE, KM, DEVELOPER, ADDRESS = (
    "Название", "Балл", "Класс", "Стадия", "Сдача", "Квартир", "Домов", "Где", "Км от центра",
    "Застройщик", "Адрес",
)  # fmt: skip
RATINGS, SITE, CHATS, SOCIAL, SEARCH, SOURCES, LAT, LON, WHY = (
    "Оценок на Картах", "Сайт", "Чаты ТГ", "Соцсети", "Искать", "Источники", "lat", "lon", "Почему",
)  # fmt: skip
COLUMNS = [
    NAME, GRADE, SCORE, STAGE, READY, FLATS, HOUSES, PLACE, KM, DEVELOPER, ADDRESS, RATINGS, SITE,
    CHATS, SOCIAL, SEARCH, SOURCES, LAT, LON, WHY,
]  # fmt: skip

# «ЖК "Озерки" поз. 34», «Клубный дом "Академический"», «Жилой комплекс «Лазурный» 2 очередь».
_PREFIX = re.compile(
    r"^(?:жк|ж\.к\.|жилой комплекс|жилой квартал|жилой район|жилой дом|клубный дом|клубный квартал|"
    r"дом|мкр\.?|микрорайон|квартал|комплекс апартаментов|апарт-комплекс|гк|кп)[\s)]+",
    re.I,
)
_UNIT = r"(?:поз|позиция|очередь|оч|этап|корпус|корп|литер|секция|блок|дом|строение|гп|уч|участок)"
# «… поз. 34», «… (поз. 5, 6)», «… 2 очередь», «…, дом 12» — хвост с номером дома или очереди.
_SUFFIX = re.compile(
    rf"[\s,.(-]*(?:\d+\s*-?\s*(?:я|й|ая|ый)?\s+)?(?<![а-яёa-z]){_UNIT}(?![а-яёa-z])[\s.№#\dа-яёa-z/,-]*\)?$",
    re.I,
)
_QUOTED = re.compile(r"[«\"“„']\s*([^«»\"“”„']{2,})")
# Названия-адреса, безымянные дома и не-жильё: в поиске Telegram по ним ничего осмысленного не найти.
_ADDRESS_LIKE = re.compile(
    r"^(?:по\s|на\s|ул\b|улиц|пер\b|переул|пр-т|просп|бульв|дом\s|жилой дом|жилые дома|многоквартирн|"
    r"группа домов|гаражн|деловой центр|жилой$|жк по\s|жк на\s|жилой комплекс\s*(?:\d|по\s)|г\.\s*воронеж|"
    r"\d+[\s-]*(?:этажн|кварт))"
    r"|\bул\.|\bд\.\s*\d|\bгск\b|,\s*\d|(?<!квартал)\s\d{2,3}\s?[а-яё]?$",
    re.I,
)
_TME = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(?:s/)?(\+?[A-Za-z0-9_-]{4,})", re.I)
_SOCIAL = re.compile(r"https?://(?:vk\.com|vk\.ru|ok\.ru)/[A-Za-z0-9_.\-/]+", re.I)
_JUNK = " \"'«»“”„.,;:-()"


def clean_name(raw: str | None) -> str:
    """«ЖК "Озерки" поз. 34» → «Озерки». Пустая строка — названия нет."""
    text = " ".join((raw or "").replace("\xa0", " ").split())
    if not text:
        return ""
    quoted = _QUOTED.search(text)
    if quoted:
        text = quoted.group(1)
    text = text.strip(_JUNK)
    # «ПОКОЛЕНИЕ, ЖК ПОКОЛЕНИЕ», «БОРОВОЕ ЖК БОРОВОЕ», «Бабяково. Новый квартал; Бабяково. Зеленый».
    text = re.split(r"\s*;\s*|,\s*жк\s|\sжк\s", text, maxsplit=1, flags=re.I)[0].strip(_JUNK) or text
    for _ in range(2):
        # «Квартал 45», «Дом на набережной»: слово-приставка здесь — часть названия.
        rest = _PREFIX.sub("", text).strip(_JUNK)
        if rest and not rest[0].isdigit() and not re.match(r"(?:на|у|в|по)\s", rest, re.I):
            text = rest
    text = _SUFFIX.sub("", text).strip(_JUNK) or text
    text = re.sub(r"\s*-\s*", "-", text)  # «Суворов- Сити»
    if text.isupper() and len(text) > 4 and not re.search(r"\d", text):
        text = text.title()  # «САБУРОВ РЕЗИДЕНС»
    return text if len(text) >= 2 and not text.isdigit() else ""


def name_key(name: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", normalize(name))


def searchable(name: str) -> bool:
    return bool(name) and not _ADDRESS_LIKE.search(name) and len(name_key(name)) >= 3


def distance_km(lat: float, lon: float, origin: tuple[float, float] = CENTER) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*origin, lat, lon))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def locality(address: str) -> str:
    """«Город Воронеж, Улица Острогожская» → «Воронеж»; «…, Село Новая Усмань, …» → «Новая Усмань»."""
    kinds = r"город|г\.|село|с\.|рабочий пос[её]лок|пос[её]лок|п\.|пгт|рп|деревня|хутор|слобода"
    found = re.findall(rf"(?:^|,\s*)(?i:{kinds})\s+([А-ЯЁ][^,]+)", address or "")
    return found[-1].strip() if found else ""


@dataclass
class Complex:
    name: str
    flats: int = 0
    houses: int = 0
    ready: list[dt.date] = field(default_factory=list)  # сроки сдачи домов
    built: list[bool] = field(default_factory=list)  # сдан ли дом (по порядку с ready)
    developers: set[str] = field(default_factory=set)
    addresses: list[str] = field(default_factory=list)
    coords: list[tuple[float, float]] = field(default_factory=list)
    ratings: int = 0
    sites: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)  # соцсети и мессенджеры с Карт и сайта
    sources: set[str] = field(default_factory=set)

    @property
    def lat_lon(self) -> tuple[float, float] | None:
        if not self.coords:
            return None
        return (
            sum(c[0] for c in self.coords) / len(self.coords),
            sum(c[1] for c in self.coords) / len(self.coords),
        )


# ---------- источники ----------


async def fetch_domrf(
    client: httpx.AsyncClient, region: int = REGION, page: int = 100, pause: float = 1
) -> list[dict]:
    """Все дома региона из реестра наш.дом.рф. Сервер сначала отвечает 307 и ставит cookie."""
    out: list[dict] = []
    offset, total = 0, None
    while total is None or offset < total:
        resp = await client.get(
            DOMRF_API,
            params={
                "offset": offset, "limit": page, "sortField": "objId", "sortType": "asc", "place": region,
            },  # fmt: skip
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        total = int(data.get("total") or 0)
        items = data.get("list") or []
        if not items:
            break
        out.extend(items)
        offset += len(items)
        await asyncio.sleep(pause)
    return out


def _date(value: object) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def add_domrf(complexes: dict[str, Complex], objects: list[dict]) -> int:
    """Дома реестра → ЖК. Возвращает, сколько домов осталось без названия ЖК."""
    unnamed = 0
    for o in objects:
        name = clean_name(o.get("objCommercNm"))
        if not name:
            unnamed += 1
            continue
        c = complexes.setdefault(name_key(name), Complex(name))
        c.sources.add("дом.рф")
        c.flats += int(o.get("objElemLivingCnt") or 0)
        c.houses += int(o.get("residentialBuildings") or 1)
        ready = _date(o.get("objReady100PercDt"))
        if ready:
            c.ready.append(ready)
            c.built.append(o.get("objStatus") == 2 or o.get("siteStatus") == "Сданные")
        dev = (o.get("developer") or {}).get("groupName") or (o.get("developer") or {}).get("shortName")
        if dev:
            c.developers.add(" ".join(str(dev).split()))
        if o.get("objAddr"):
            c.addresses.append(str(o["objAddr"]))
        if o.get("latitude") and o.get("longitude"):
            c.coords.append((float(o["latitude"]), float(o["longitude"])))
    return unnamed


def merge_phases(complexes: dict[str, Complex]) -> int:
    """«Галактика 2», «Галактика 3» → «Галактика», если такой ЖК есть. Возвращает число слияний."""
    merged = 0
    for key in sorted(complexes, key=len, reverse=True):
        m = re.fullmatch(r"(.{4,}?)\d", key)
        base = complexes.get(m.group(1)) if m else None
        if base is None:
            continue
        part = complexes.pop(key)
        base.flats += part.flats
        base.houses += part.houses
        base.ready += part.ready
        base.built += part.built
        base.developers |= part.developers
        base.addresses += part.addresses
        base.coords += part.coords
        base.ratings = max(base.ratings, part.ratings)
        base.sites += [u for u in part.sites if u not in base.sites]
        base.links += [u for u in part.links if u not in base.links]
        base.sources |= part.sources
        merged += 1
    return merged


def add_yandex(complexes: dict[str, Complex], items: list[dict], near_m: float = 250) -> tuple[int, int]:
    """Организации категории «Жилой комплекс» с Карт. Возвращает (совпало с реестром, добавлено новых)."""
    matched = added = 0
    for it in items:
        cats = [str(c).lower() for c in it.get("cat") or []]
        if "жилой комплекс" not in cats or it.get("st") == "closed":
            continue
        name = clean_name(it.get("t"))
        if not name:
            continue
        lon, lat = (it.get("c") or [None, None])[:2]
        if lat and lon and distance_km(lat, lon) > 300:
            continue  # по слову «Борисоглебск» Карты приносят и подмосковное «Борисоглебское»
        c = complexes.get(name_key(name))
        if c is None and lat and lon and not searchable(name):
            # «По ул. Одесская» — это может быть дом уже известного ЖК: ищем ближайший из реестра.
            best = min(
                (x for x in complexes.values() if x.lat_lon and "дом.рф" in x.sources),
                key=lambda x: distance_km(lat, lon, x.lat_lon),
                default=None,
            )
            if best and distance_km(lat, lon, best.lat_lon) * 1000 <= near_m:
                c = best
        if c is None:
            c = complexes[name_key(name)] = Complex(name)
            added += 1
        elif "дом.рф" in c.sources and "яндекс" not in c.sources:
            matched += 1
        c.sources.add("яндекс")
        c.ratings = max(c.ratings, int(it.get("rc") or 0))
        if lat and lon and "дом.рф" not in c.sources:
            c.coords.append((float(lat), float(lon)))
        if it.get("a") and "дом.рф" not in c.sources:
            c.addresses.append(str(it["a"]))
        for url in it.get("u") or []:
            if url not in c.sites:
                c.sites.append(url)
        for url in it.get("l") or []:
            if url not in c.links:
                c.links.append(url)
    return matched, added


# ---------- оценка ----------


def stage_of(c: Complex, today: dt.date) -> tuple[str, int, str]:
    """(стадия, баллы 0–45, пояснение). Берётся лучшая из стадий домов: ЖК сдают очередями."""
    best = ("", 0, "")

    def offer(stage: str, points: int, why: str) -> None:
        nonlocal best
        if points > best[1]:
            best = (stage, points, why)

    for ready, built in zip(c.ready, c.built, strict=False):
        months = (today.year - ready.year) * 12 + today.month - ready.month
        if built or months > 0:
            # Сдан (или срок прошёл): чем свежее, тем больше въезжающих и доставок мебели.
            if months <= 12:
                offer("заселяется", 45, "сдан меньше года назад — идёт заселение")
            elif months <= 24:
                offer("заселяется", 38, "сдан 1–2 года назад — ещё въезжают и делают ремонт")
            elif months <= 48:
                offer("сдан", 26, "сдан 2–4 года назад")
            else:
                offer("сдан", 16, "сдан больше 4 лет назад")
        elif months >= -6:
            offer("скоро сдача", 34, "сдача в ближайшие полгода — скоро въезд")
        elif months >= -18:
            offer("строится", 14, "строится, сдача через 0,5–1,5 года")
        else:
            offer("строится", 6, "строится, сдача нескоро")
    if not best[0]:
        return "сдан", 14, "нет в реестре новостроек — дом старше или без декларации"
    if best[0] in ("заселяется", "сдан") and any(not b for b in c.built):
        return best[0], min(45, best[1] + 4), best[2] + ", строятся новые очереди"
    return best


def size_points(c: Complex) -> tuple[int, str]:
    """Баллы 0–35 за размер: по числу квартир, а если его нет — по числу оценок на Картах."""
    if c.flats:
        points = round(min(35, max(4, 9.5 * math.log10(c.flats) - 2)))
        return points, f"{c.flats} кв."
    for limit, points in ((1000, 24), (300, 19), (100, 15), (30, 11), (1, 8)):
        if c.ratings >= limit:
            return points, f"{c.ratings} оценок на Картах"
    return 6, "размер неизвестен"


def geo_points(c: Complex) -> tuple[int, float | None, str]:
    """Баллы 0–20 за близость к Воронежу."""
    pos = c.lat_lon
    if pos is None:
        return 8, None, ""
    km = distance_km(*pos)
    if km <= 15:
        return 20, km, "Воронеж"
    if km <= 30:
        return 15, km, "пригород"
    if km <= 60:
        return 8, km, "до 60 км"
    return 2, km, "дальний район области"


def grade(score: int) -> str:
    return "A" if score >= 75 else "B" if score >= 58 else "C" if score >= 40 else "D"


def chat_links(c: Complex) -> list[str]:
    """Публичные ссылки t.me из соцсетей ЖК (чат это или канал застройщика — выяснит разведка)."""
    out = []
    for url in c.links:
        m = _TME.search(url)
        link = normalize_link(m.group(0)) if m else None
        if link and link not in out:
            out.append(link)
    return out


def to_row(c: Complex, today: dt.date) -> dict[str, str]:
    stage, s_points, s_why = stage_of(c, today)
    z_points, z_why = size_points(c)
    g_points, km, g_why = geo_points(c)
    score = s_points + z_points + g_points
    pos = c.lat_lon
    place = next((p for p in map(locality, c.addresses) if p), "")
    ready = ""
    if c.ready:
        first, last = min(c.ready), max(c.ready)
        ready = first.isoformat() if first == last else f"{first.isoformat()} … {last.isoformat()}"
    social = [u for u in c.links if not _TME.search(u)]
    return {
        NAME: c.name,
        GRADE: grade(score),
        SCORE: str(score),
        STAGE: stage,
        READY: ready,
        FLATS: str(c.flats) if c.flats else "",
        HOUSES: str(c.houses) if c.houses else "",
        PLACE: place,
        KM: f"{km:.0f}" if km is not None else "",
        DEVELOPER: ", ".join(sorted(c.developers)),
        ADDRESS: c.addresses[0] if c.addresses else "",
        RATINGS: str(c.ratings) if c.ratings else "",
        SITE: c.sites[0] if c.sites else "",
        CHATS: " ".join(chat_links(c)),
        SOCIAL: " ".join(social[:4]),
        SEARCH: "да" if searchable(c.name) else "нет",
        SOURCES: ", ".join(sorted(c.sources)),
        LAT: f"{pos[0]:.5f}" if pos else "",
        LON: f"{pos[1]:.5f}" if pos else "",
        WHY: "; ".join(p for p in (s_why, z_why, g_why) if p),
    }


def build_rows(complexes: dict[str, Complex], today: dt.date) -> list[dict[str, str]]:
    rows = [to_row(c, today) for c in complexes.values()]
    rows.sort(key=lambda r: (-int(r[SCORE]), -int(r[FLATS] or 0), r[NAME]))
    return rows


def dumps(rows: list[dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return "﻿" + buf.getvalue()  # BOM — чтобы файл сразу открывался в Excel


# ---------- чтение готового списка (для разведки) ----------


def load_rows_text(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return load_rows_text(path.read_text(encoding="utf-8-sig"))


def search_names(rows: list[dict[str, str]], min_grade: str = "D") -> list[str]:
    """Названия для поисковых запросов, в порядке файла (лучшие — первыми)."""
    return [r[NAME] for r in rows if r.get(SEARCH) == "да" and r.get(GRADE, "D") <= min_grade and r.get(NAME)]


def seed_links(rows: list[dict[str, str]]) -> list[str]:
    """Ссылки на чаты, найденные вместе с ЖК, — разведка проверяет их первыми, без поиска."""
    out: list[str] = []
    for r in rows:
        for link in (r.get(CHATS) or "").split():
            if link not in out:
                out.append(link)
    return out


# ---------- сайты ЖК: ссылки на Telegram ----------


async def crawl_sites(client: httpx.AsyncClient, complexes: dict[str, Complex], limit: int = 400) -> int:
    """Главная страница сайта ЖК → ссылки t.me, vk.com, ok.ru. Возвращает число сайтов с находками."""
    hits = 0
    seen: dict[str, list[str]] = {}
    for c in list(complexes.values()):
        for site in c.sites[:1]:
            if site not in seen:
                if len(seen) >= limit:
                    break
                try:
                    resp = await client.get(site, timeout=15)
                    text = resp.text if resp.status_code == 200 else ""
                except (httpx.HTTPError, UnicodeError, ValueError):
                    text = ""
                found = [f"https://t.me/{m}" for m in dict.fromkeys(_TME.findall(text))]
                found += list(dict.fromkeys(_SOCIAL.findall(text)))[:3]
                seen[site] = found
                hits += bool(found)
                await asyncio.sleep(0.5)
            for url in seen[site]:
                if url not in c.links:
                    c.links.append(url)
    return hits


# ---------- запуск ----------


def load_yandex(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    items = json.loads(path.read_text(encoding="utf-8"))
    return items if isinstance(items, list) else list(items.values())


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


async def collect(yandex_file: Path | None, today: dt.date, crawl: bool = True) -> tuple[list[dict[str, str]], str]:
    complexes: dict[str, Complex] = {}
    headers = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=40) as client:
        objects = await fetch_domrf(client)
        unnamed = add_domrf(complexes, objects)
        merge_phases(complexes)
        from_registry = len(complexes)
        matched = added = 0
        items = await asyncio.to_thread(load_yandex, yandex_file)
        if items:
            matched, added = add_yandex(complexes, items)
        merge_phases(complexes)
        sites = await crawl_sites(client, complexes) if crawl else 0
    rows = build_rows(complexes, today)
    grades = {g: sum(r[GRADE] == g for r in rows) for g in "ABCD"}
    report = (
        f"дом.рф: домов {len(objects)} (без названия ЖК {unnamed}) → ЖК {from_registry}; "
        f"Яндекс Карты: совпало {matched}, новых {added}; сайтов со ссылками {sites}; "
        f"всего ЖК {len(rows)}: " + ", ".join(f"{g} — {n}" for g, n in grades.items())
        + f"; со ссылкой на Telegram {sum(bool(r[CHATS]) for r in rows)}"
    )  # fmt: skip
    return rows, report


async def main() -> None:
    from app.config import Settings
    from app.leads.datarepo import DataRepo

    parser = argparse.ArgumentParser(description="Собрать список ЖК Воронежа и области")
    parser.add_argument("--out", type=Path, help="куда записать CSV вместо репозитория данных")
    parser.add_argument("--yandex", type=Path, help="выгрузка Яндекс Карт (по умолчанию — из репозитория данных)")
    parser.add_argument("--no-crawl", action="store_true", help="не ходить на сайты ЖК")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    today = dt.datetime.now(dt.UTC).date()
    if args.out:
        rows, report = await collect(args.yandex, today, not args.no_crawl)
        await asyncio.to_thread(_write, args.out, dumps(rows))
    else:
        repo = DataRepo(
            settings.DATA_DIR / "leads-data", settings.LEADS_DATA_REPO, settings.leads_data_key,
            settings.LEADS_DATA_BRANCH,
        )  # fmt: skip
        await repo.read()  # свежая копия: в ней лежит выгрузка Карт
        rows, report = await collect(args.yandex or repo.path / YANDEX_PATH, today, not args.no_crawl)
        warning = await repo.put_file(CSV_PATH, dumps(rows), f"complexes: {len(rows)} ЖК")
        if warning:
            log.warning(warning)
    log.info(report)
    for r in rows[:15]:
        log.info("%s %s  %s — %s", r[GRADE], r[SCORE], r[NAME], r[WHY])


if __name__ == "__main__":
    asyncio.run(main())
