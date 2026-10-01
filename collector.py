#!/usr/bin/env python3
"""Collect historical Arena of Valor / Honor of Kings teams and people.

The collector intentionally uses the public MediaWiki API, checkpoints every
response, and applies Liquipedia's published request limits. It parses rendered
team pages because historical player/staff tables are generated dynamically.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


ROLE_WORDS = {
    "coach", "head coach", "assistant coach", "strategic coach", "manager",
    "analyst", "owner", "ceo", "founder", "director", "staff", "translator",
}
PLAYER_ROLE_WORDS = {
    "top lane", "jungler", "jungle", "middle", "mid lane", "bottom lane",
    "abyssal dragon lane", "roamer", "support", "substitute", "player",
}
TEAM_TABLE_HINTS = {"active", "former", "squad", "roster", "organization"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def slugify(value: str) -> str:
    value = value.replace("Đ", "D").replace("đ", "d")
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug or "unknown"


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in fields})


@dataclass
class Limits:
    api_delay: float
    parse_delay: float
    image_delay: float
    max_api_per_hour: int


class RateLimiter:
    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self.last_by_kind: dict[str, float] = defaultdict(float)
        self.api_window: deque[float] = deque()

    def wait(self, kind: str) -> None:
        delay = {
            "api": self.limits.api_delay,
            "parse": self.limits.parse_delay,
            "image": self.limits.image_delay,
        }[kind]
        current = time.monotonic()
        pause = delay - (current - self.last_by_kind[kind])
        if pause > 0:
            time.sleep(pause)
        if kind in {"api", "parse"}:
            current = time.monotonic()
            while self.api_window and current - self.api_window[0] >= 3600:
                self.api_window.popleft()
            if len(self.api_window) >= self.limits.max_api_per_hour:
                time.sleep(max(0.0, 3600 - (current - self.api_window[0]) + 1))
                current = time.monotonic()
                while self.api_window and current - self.api_window[0] >= 3600:
                    self.api_window.popleft()
            self.api_window.append(time.monotonic())
        self.last_by_kind[kind] = time.monotonic()


class WikiClient:
    def __init__(self, config: dict[str, Any], root: Path) -> None:
        self.config = config
        self.root = root
        self.cache = root / ".cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config["user_agent"],
            "Accept": "application/json",
        })
        self.rate = RateLimiter(Limits(
            config["api_delay_seconds"],
            config["parse_delay_seconds"],
            config["image_delay_seconds"],
            config["max_api_requests_per_hour"],
        ))

    def _get_json(self, params: dict[str, Any], cache_key: str, kind: str = "api", api_url: str | None = None) -> dict[str, Any]:
        cache_path = self.cache / f"{slugify(cache_key)}.json"
        if self.config.get("resume", True) and cache_path.exists():
            return read_json(cache_path, {})
        self.rate.wait(kind)
        response = self.session.get(api_url or self.config["api_url"], params=params, timeout=90)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(json.dumps(data["error"], ensure_ascii=False))
        write_json(cache_path, data)
        return data

    def category_members(self, category: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        continuation: dict[str, Any] = {}
        page_no = 0
        while True:
            params = {
                "action": "query", "format": "json", "formatversion": 2,
                "list": "categorymembers", "cmtitle": f"Category:{category}",
                "cmnamespace": "0|14", "cmlimit": "max", "cmprop": "ids|title|type",
                **continuation,
            }
            data = self._get_json(params, f"category-{category}-{page_no}")
            rows.extend(data.get("query", {}).get("categorymembers", []))
            continuation = data.get("continue", {})
            if not continuation:
                break
            page_no += 1
        return rows

    def category_tree(self, roots: Iterable[str]) -> list[dict[str, Any]]:
        pages: dict[int, dict[str, Any]] = {}
        queue = deque(roots)
        seen_categories: set[str] = set()
        while queue:
            category = queue.popleft()
            if category in seen_categories:
                continue
            seen_categories.add(category)
            for item in self.category_members(category):
                if self.config.get("recursive_categories", False) and item.get("ns") == 14 and item["title"].startswith("Category:"):
                    queue.append(item["title"].split(":", 1)[1])
                elif item.get("ns") == 0:
                    pages[item["pageid"]] = item
        return sorted(pages.values(), key=lambda item: item["title"].casefold())

    def image_info(self, urls: Iterable[str]) -> dict[str, dict[str, Any]]:
        titles_by_url = {url: commons_file_title(url) for url in urls if url}
        titles = sorted({title for title in titles_by_url.values() if title})
        info_by_title: dict[str, dict[str, Any]] = {}
        for start in range(0, len(titles), 20):
            batch = titles[start:start + 20]
            params = {
                "action": "query", "format": "json", "formatversion": 2,
                "prop": "imageinfo", "titles": "|".join(batch),
                "iiprop": "url|mime|size|sha1|extmetadata",
            }
            key = "imageinfo-" + hashlib.sha1("|".join(batch).encode()).hexdigest()
            data = self._get_json(params, key, api_url=self.config.get("commons_api_url"))
            normalized = {
                item.get("from"): item.get("to")
                for item in data.get("query", {}).get("normalized", [])
            }
            for page in data.get("query", {}).get("pages", []):
                if page.get("imageinfo"):
                    raw = page["imageinfo"][0]
                    ext = raw.get("extmetadata", {})
                    info_by_title[page["title"]] = {
                        "original_url": raw.get("url"), "mime": raw.get("mime"),
                        "width": raw.get("width"), "height": raw.get("height"),
                        "source_sha1": raw.get("sha1"),
                        "license": (ext.get("LicenseShortName") or {}).get("value"),
                        "license_url": (ext.get("LicenseUrl") or {}).get("value"),
                        "artist": clean_text((ext.get("Artist") or {}).get("value")),
                        "credit": clean_text((ext.get("Credit") or {}).get("value")),
                        "copyrighted": (ext.get("Copyrighted") or {}).get("value"),
                        "commons_page": page.get("title"),
                    }
            for source_title, normalized_title in normalized.items():
                if normalized_title in info_by_title:
                    info_by_title[source_title] = info_by_title[normalized_title]
        return {
            url: info_by_title.get(title, {"original_url": url, "commons_page": title})
            for url, title in titles_by_url.items()
        }

    def parse_page(self, title: str) -> dict[str, Any]:
        params = {
            "action": "parse", "format": "json", "formatversion": 2,
            "page": title, "prop": "text|revid|displaytitle|images|categories|properties",
            "disabletoc": 1,
        }
        return self._get_json(params, f"parse-{title}", kind="parse").get("parse", {})

    def download(self, url: str, destination: Path) -> dict[str, Any]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.config.get("resume", True) and destination.exists() and destination.stat().st_size:
            raw = destination.read_bytes()
            return {"path": str(destination), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        self.rate.wait("image")
        response = self.session.get(url, timeout=120, headers={"Accept": "image/*"})
        response.raise_for_status()
        destination.write_bytes(response.content)
        return {
            "path": str(destination), "sha256": hashlib.sha256(response.content).hexdigest(),
            "bytes": len(response.content), "content_type": response.headers.get("content-type"),
        }


def absolute_image_url(src: str | None) -> str | None:
    if not src or src.startswith("data:"):
        return None
    if src.startswith("//"):
        return "https:" + src
    return urljoin("https://liquipedia.net", src)


def commons_file_title(url: str) -> str | None:
    parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
    if "images" not in parts:
        return None
    if "thumb" in parts and len(parts) >= 2:
        filename = parts[-2]
    else:
        filename = parts[-1]
    return "File:" + filename if "." in filename else None


def best_image_url(img: Tag | None) -> str | None:
    if img is None:
        return None
    srcset = img.get("srcset", "")
    candidates = []
    for part in srcset.split(","):
        bits = part.strip().split()
        if bits:
            candidates.append(bits[0])
    return absolute_image_url(candidates[-1] if candidates else img.get("src"))


def remove_placeholder_image(url: str | None) -> str | None:
    if not url:
        return None
    lowered = unquote(url).replace("_", " ").casefold()
    return None if "noimage" in lowered or "no image" in lowered else url


def page_link_from_href(href: str | None) -> str | None:
    if not href or "/honorofkings/" not in href:
        return None
    parsed = urlparse(href)
    tail = parsed.path.split("/honorofkings/", 1)[1] if "/honorofkings/" in parsed.path else ""
    if tail == "index.php":
        title = parse_qs(parsed.query).get("title", [""])[0]
    else:
        title = tail.split("#", 1)[0]
    title = unquote(title)
    if not title or ":" in title:
        return None
    return title.replace("_", " ")


def extract_infobox(soup: BeautifulSoup) -> dict[str, Any]:
    box = soup.select_one(".fo-nttax-infobox, .infobox")
    result: dict[str, Any] = {}
    if not box:
        return result
    title_node = box.select_one(".infobox-header, .infobox-title")
    if title_node:
        direct_text = clean_text(" ".join(str(node) for node in title_node.find_all(string=True, recursive=False)))
        result["display_name"] = direct_text or clean_text(title_node.get_text(" ", strip=True))
    image = box.select_one(".infobox-image img") or box.select_one("img")
    if image:
        result["image_url"] = best_image_url(image)
    for label_node in box.select(".infobox-description"):
        parent = label_node.parent
        value_node = label_node.find_next_sibling()
        if value_node is None and parent:
            children = parent.find_all("div", recursive=False)
            value_node = children[1] if len(children) > 1 else None
        if value_node:
            key = slugify(clean_text(label_node.get_text(" ", strip=True))).replace("-", "_")
            if key:
                result[key] = clean_text(value_node.get_text(" ", strip=True))
    for row in box.select("tr"):
        label_node = row.select_one("th")
        value_node = row.select_one("td")
        if label_node and value_node:
            key = slugify(clean_text(label_node.get_text(" ", strip=True))).replace("-", "_")
            if key:
                result[key] = clean_text(value_node.get_text(" ", strip=True))
    return result


def nearest_heading(table: Tag) -> str:
    heading = table.find_previous(["h2", "h3", "h4"])
    return clean_text(heading.get_text(" ", strip=True) if heading else "")


def table_rows(table: Tag) -> list[dict[str, Any]]:
    header_cells = table.select("tr th")
    first_row = table.select_one("tr")
    if not first_row:
        return []
    headers = [clean_text(cell.get_text(" ", strip=True)) for cell in first_row.find_all(["th", "td"], recursive=False)]
    if not headers or not any(headers):
        return []
    rows: list[dict[str, Any]] = []
    for tr in table.select("tr")[1:]:
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) < 2:
            continue
        values = [clean_text(cell.get_text(" ", strip=True)) for cell in cells]
        row = {headers[i] if i < len(headers) and headers[i] else f"column_{i+1}": values[i] for i in range(len(values))}
        row["_links"] = [
            {"text": clean_text(a.get_text(" ", strip=True)), "title": page_link_from_href(a.get("href")), "href": a.get("href")}
            for a in tr.select("a[href]") if page_link_from_href(a.get("href"))
        ]
        row["_flags"] = [
            {"region": clean_text(img.get("alt") or img.get("title")), "url": best_image_url(img)}
            for img in tr.select(".flag img")
            if clean_text(img.get("alt") or img.get("title")) and best_image_url(img)
        ]
        rows.append(row)
    return rows


def pick(row: dict[str, Any], *needles: str) -> str | None:
    for key, value in row.items():
        normalized = slugify(key)
        if any(needle in normalized for needle in needles) and not key.startswith("_"):
            return clean_text(str(value)) or None
    return None


def infer_person_title(row: dict[str, Any]) -> str | None:
    links = row.get("_links", [])
    if not links:
        return None
    id_text = pick(row, "id", "nickname", "player")
    if id_text:
        for link in links:
            if link["text"].casefold() == id_text.casefold():
                return link["title"]
    return links[0]["title"]


def detect_games(soup: BeautifulSoup) -> list[str]:
    games: set[str] = set()
    for link in soup.select("a[href]"):
        href = unquote(link.get("href") or "").replace("_", " ").casefold()
        title = clean_text(link.get("title")).replace("_", " ").casefold()
        if "arena of valor" in href or "arena of valor" in title:
            games.add("Arena of Valor")
        if "honor of kings" in href or "honor of kings" in title:
            games.add("Honor of Kings")
    return sorted(games)


def classify_role(role: str | None, heading: str) -> str:
    haystack = f"{role or ''} {heading}".casefold()
    if any(word in haystack for word in ROLE_WORDS) or "organization" in haystack:
        return "staff"
    if any(word in haystack for word in PLAYER_ROLE_WORDS) or "squad" in haystack or "roster" in haystack:
        return "player"
    return "member"


def parse_team(title: str, parsed: dict[str, Any], base_url: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, str]]]:
    html = parsed.get("text", "")
    soup = BeautifulSoup(html, "html.parser")
    info = extract_infobox(soup)
    team_id = stable_id("team", title.casefold())
    created_history = info.get("created")
    created_dates = re.findall(r"\d{4}-\d{2}-\d{2}", created_history or "")
    team = {
        "team_id": team_id, "page_title": title,
        "name": info.get("display_name") or title,
        "location": info.get("location") or info.get("country"),
        "region": info.get("region"), "founded": info.get("founded") or (created_dates[0] if created_dates else None),
        "created_history": created_history,
        "disbanded": info.get("disbanded"), "status": info.get("status"),
        "games": detect_games(soup),
        "logo_url": info.get("image_url"),
        "source_url": base_url + quote(title.replace(" ", "_"), safe="/_"),
        "source_revision": parsed.get("revid"), "collected_at": now_iso(),
    }
    memberships: list[dict[str, Any]] = []
    regions: list[dict[str, str]] = []
    for table in soup.select("table"):
        heading = nearest_heading(table)
        if not any(hint in heading.casefold() for hint in TEAM_TABLE_HINTS):
            continue
        for row in table_rows(table):
            person_title = infer_person_title(row)
            ign = pick(row, "id", "nickname", "player")
            real_name = pick(row, "name")
            role = pick(row, "position", "role")
            if not person_title and not ign and not real_name:
                continue
            person_key = person_title or ign or real_name or "unknown"
            person_id = stable_id("person", person_key.casefold())
            state = "former" if "former" in heading.casefold() else "active"
            membership = {
                "membership_id": stable_id("membership", "|".join([
                    team_id, person_id, role or "", pick(row, "join") or "", pick(row, "leave") or "", state,
                ])),
                "team_id": team_id, "person_id": person_id, "person_page_title": person_title,
                "ign": ign, "real_name": real_name, "member_type": classify_role(role, heading),
                "role": role, "status": state, "join_date": pick(row, "join"),
                "leave_date": pick(row, "leave"), "new_team": pick(row, "new-team", "newteam"),
                "source_url": team["source_url"], "source_revision": parsed.get("revid"),
            }
            memberships.append(membership)
            for flag in row.get("_flags", []):
                if flag.get("region") and flag.get("url"):
                    regions.append(flag)
    return team, memberships, regions


def parse_person(title: str, parsed: dict[str, Any], base_url: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    soup = BeautifulSoup(parsed.get("text", ""), "html.parser")
    info = extract_infobox(soup)
    person_id = stable_id("person", title.casefold())
    person = {
        "person_id": person_id, "page_title": title,
        "ign": info.get("id") or info.get("nickname") or info.get("display_name") or title,
        "real_name": info.get("name") or info.get("real_name"),
        "nationality": info.get("nationality") or info.get("country"),
        "birth_date": info.get("born") or info.get("birth_date"),
        "status": info.get("status"), "role": info.get("role") or info.get("roles") or info.get("position"),
        "team": info.get("team"), "portrait_url": remove_placeholder_image(info.get("image_url")),
        "source_url": base_url + quote(title.replace(" ", "_"), safe="/_"),
        "source_revision": parsed.get("revid"), "collected_at": now_iso(),
    }
    regions = []
    for img in soup.select(".fo-nttax-infobox .flag img, .infobox .flag img"):
        region = clean_text(img.get("alt") or img.get("title"))
        url = best_image_url(img)
        if region and url and region.casefold() != clean_text(person["ign"]).casefold():
            regions.append({"region": region, "url": url})
    return person, regions


def image_extension(url: str, content_type: str | None = None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}:
        return suffix
    return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/svg+xml": ".svg"}.get(content_type or "", ".img")


def unique_by(rows: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get(key):
            result[str(row[key])] = row
    return list(result.values())


def save_outputs(output: Path, teams: list[dict[str, Any]], people: list[dict[str, Any]], memberships: list[dict[str, Any]], regions: list[dict[str, Any]]) -> None:
    staff = [p for p in people if any(m["person_id"] == p["person_id"] and m["member_type"] == "staff" for m in memberships)]
    players = [p for p in people if any(m["person_id"] == p["person_id"] and m["member_type"] == "player" for m in memberships)]
    payloads = {
        "teams": teams, "people": people, "players": players, "staff": staff,
        "memberships": memberships, "regions": regions,
    }
    for name, rows in payloads.items():
        write_json(output / "json" / f"{name}.json", rows)
        write_csv(output / "csv" / f"{name}.csv", rows)
    write_json(output / "manifest.json", {
        "schema_version": "1.0", "collected_at": now_iso(),
        "counts": {name: len(rows) for name, rows in payloads.items()},
        "license_note": "Source text/data: Liquipedia, CC BY-SA; images retain their original licenses.",
    })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--limit-teams", type=int, default=0)
    parser.add_argument("--limit-people", type=int, default=0)
    parser.add_argument("--team-title", action="append", default=[])
    parser.add_argument("--person-title", action="append", default=[])
    parser.add_argument("--skip-images", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    config = read_json(root / args.config, {})
    output = root / config.get("output_dir", "output")
    output.mkdir(parents=True, exist_ok=True)
    client = WikiClient(config, root)

    state_path = output / "state.json"
    state = read_json(state_path, {"teams": {}, "people": {}})
    state.setdefault("teams", {})
    state.setdefault("people", {})
    team_pages = (
        [{"title": title} for title in args.team_title]
        if args.team_title else client.category_tree(config["categories"]["teams"])
    )
    # Merge each batch with the previous output. GitHub-hosted runners are
    # ephemeral, so the workflow restores this directory from Actions cache.
    teams: list[dict[str, Any]] = read_json(output / "json" / "teams.json", [])
    memberships: list[dict[str, Any]] = read_json(output / "json" / "memberships.json", [])
    people: list[dict[str, Any]] = read_json(output / "json" / "people.json", [])
    old_regions: list[dict[str, Any]] = read_json(output / "json" / "regions.json", [])
    region_map: dict[str, dict[str, Any]] = {
        slugify(row.get("name") or row.get("region_id") or "unknown"): {
            "region": row.get("name"), "url": row.get("image_url"),
        }
        for row in old_regions if row.get("name") and row.get("image_url")
    }
    discovered_people: set[str] = {
        row["person_page_title"] for row in memberships if row.get("person_page_title")
    }
    teams_processed_this_run = 0

    for index, page in enumerate(team_pages, 1):
        title = page["title"]
        if not args.team_title and state["teams"].get(title, {}).get("done"):
            continue
        parsed = client.parse_page(title)
        team, member_rows, flags = parse_team(title, parsed, config["base_url"])
        requested_games = set(config.get("games", []))
        if requested_games and not requested_games.intersection(team.get("games", [])):
            state["teams"][title] = {"done": True, "included": False, "games": team.get("games", []), "revision": parsed.get("revid"), "at": now_iso()}
            write_json(state_path, state)
            print(f"[skip non-AoV {index}/{len(team_pages)}] {title}", flush=True)
        else:
            teams.append(team)
            memberships.extend(member_rows)
            discovered_people.update(m["person_page_title"] for m in member_rows if m.get("person_page_title"))
            for flag in flags:
                region_map[slugify(flag["region"])] = flag
            state["teams"][title] = {"done": True, "included": True, "games": team.get("games", []), "revision": parsed.get("revid"), "at": now_iso()}
            write_json(state_path, state)
            print(f"[team {index}/{len(team_pages)}] {title}", flush=True)
        teams_processed_this_run += 1
        if args.limit_teams and teams_processed_this_run >= args.limit_teams:
            break

    roster_people = set(discovered_people)
    category_people = client.category_tree(config["categories"]["players"]) if config.get("include_player_category", False) else []
    category_person_titles = {page["title"] for page in category_people}
    person_titles = (
        args.person_title
        if args.person_title
        else sorted(roster_people, key=str.casefold) + sorted(category_person_titles - roster_people, key=str.casefold)
    )
    people_processed_this_run = 0
    for index, title in enumerate(person_titles, 1):
        if not args.person_title and state["people"].get(title, {}).get("done"):
            continue
        try:
            parsed = client.parse_page(title)
        except RuntimeError as error:
            if "missingtitle" not in str(error):
                raise
            state["people"][title] = {"done": True, "included": False, "reason": "missingtitle", "at": now_iso()}
            write_json(state_path, state)
            print(f"[skip missing person {index}/{len(person_titles)}] {title}", flush=True)
            continue
        person, flags = parse_person(title, parsed, config["base_url"])
        people.append(person)
        for flag in flags:
            region_map[slugify(flag["region"])] = flag
        state["people"][title] = {"done": True, "revision": parsed.get("revid"), "at": now_iso()}
        write_json(state_path, state)
        print(f"[person {index}/{len(person_titles)}] {title}", flush=True)
        people_processed_this_run += 1
        if args.limit_people and people_processed_this_run >= args.limit_people:
            break

    people_by_id = {p["person_id"]: p for p in people}
    for membership in memberships:
        if membership["person_id"] not in people_by_id:
            people_by_id[membership["person_id"]] = {
                "person_id": membership["person_id"], "page_title": membership.get("person_page_title"),
                "ign": membership.get("ign"), "real_name": membership.get("real_name"),
                "source_url": membership.get("source_url"), "collected_at": now_iso(),
            }
    people = list(people_by_id.values())
    regions = [
        {"region_id": stable_id("region", key), "name": item["region"], "image_url": item["url"]}
        for key, item in sorted(region_map.items())
    ]

    if config.get("download_images", True) and not args.skip_images:
        image_manifest: list[dict[str, Any]] = []
        all_image_urls = {
            *(team["logo_url"] for team in teams if team.get("logo_url")),
            *(person["portrait_url"] for person in people if person.get("portrait_url")),
            *(region["image_url"] for region in regions if region.get("image_url")),
        }
        image_info = client.image_info(all_image_urls)
        for team in teams:
            if team.get("logo_url"):
                license_meta = image_info.get(team["logo_url"], {})
                download_url = license_meta.get("original_url") or team["logo_url"]
                stem = slugify(team["name"])
                ext = image_extension(download_url, license_meta.get("mime"))
                dest = output / "images" / "teams" / f"{stem}{ext}"
                meta = client.download(download_url, dest)
                team["logo_path"] = str(dest.relative_to(output))
                image_manifest.append({"kind": "team", "owner_id": team["team_id"], "source_url": team["logo_url"], **license_meta, **meta})
        team_by_id = {team["team_id"]: team for team in teams}
        person_memberships: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for membership in memberships:
            person_memberships[membership["person_id"]].append(membership)
        for person in people:
            if not person.get("portrait_url"):
                continue
            license_meta = image_info.get(person["portrait_url"], {})
            download_url = license_meta.get("original_url") or person["portrait_url"]
            ext = image_extension(download_url, license_meta.get("mime"))
            canonical = output / "images" / "people" / f"{slugify(person.get('ign') or person.get('page_title') or person['person_id'])}{ext}"
            meta = client.download(download_url, canonical)
            person["portrait_path"] = str(canonical.relative_to(output))
            image_manifest.append({"kind": "person", "owner_id": person["person_id"], "source_url": person["portrait_url"], **license_meta, **meta})
            if config.get("duplicate_portraits_per_team", True):
                for membership in person_memberships.get(person["person_id"], []):
                    team = team_by_id.get(membership["team_id"])
                    if not team:
                        continue
                    filename = f"{slugify(team['name'])}-{slugify(person.get('ign') or person.get('page_title') or person['person_id'])}{ext}"
                    alias = output / "images" / "people-by-team" / filename
                    alias.parent.mkdir(parents=True, exist_ok=True)
                    if not alias.exists():
                        shutil.copy2(canonical, alias)
        for region in regions:
            license_meta = image_info.get(region["image_url"], {})
            download_url = license_meta.get("original_url") or region["image_url"]
            ext = image_extension(download_url, license_meta.get("mime"))
            dest = output / "images" / "regions" / f"{slugify(region['name'])}{ext}"
            meta = client.download(download_url, dest)
            region["image_path"] = str(dest.relative_to(output))
            image_manifest.append({"kind": "region", "owner_id": region["region_id"], "source_url": region["image_url"], **license_meta, **meta})
        write_json(output / "image-manifest.json", image_manifest)

    teams = unique_by(teams, "team_id")
    people = unique_by(people, "person_id")
    memberships = unique_by(memberships, "membership_id")
    regions = unique_by(regions, "region_id")
    state["progress"] = {
        "known_team_pages": len(team_pages),
        "finished_team_pages": sum(
            1 for page in team_pages
            if state["teams"].get(page["title"], {}).get("done")
        ),
        "known_people": len(person_titles),
        "finished_people": sum(
            1 for title in person_titles
            if state["people"].get(title, {}).get("done")
        ),
        "teams_processed_this_run": teams_processed_this_run,
        "people_processed_this_run": people_processed_this_run,
        "updated_at": now_iso(),
    }
    write_json(state_path, state)
    save_outputs(output, teams, people, memberships, regions)
    print(json.dumps(read_json(output / "manifest.json", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
