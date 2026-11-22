"""Build a local, read-only HTML snapshot from the running parser API."""

import argparse
import json
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings


def safe_url(value):
    if value and urlsplit(value).scheme in {"http", "https"}:
        return escape(value, quote=True)
    return "#"


def build_report(articles, job):
    cards = []
    stored = sum(bool(image["file_id"]) for article in articles for image in article["images"])
    image_count = sum(len(article["images"]) for article in articles)
    storage_notice = (
        "Все изображения сохранены в локальном file_service. Обложки и ссылки ниже "
        "используют его API, а не сайт ФСП. Экспорт в news_service в этом тесте не выполнялся."
        if stored == image_count and image_count
        else "Несохранённые изображения показаны напрямую с сайта ФСП. "
        "Это предпросмотр исходников. Для хранения нужен разрешённый FILE_SERVICE_TOKEN. "
        "Экспорт в news_service в этом тесте не выполнялся."
    )
    for article in articles:
        pictures = article["images"]
        cover = pictures[0] if pictures else None
        cover_html = ""
        if cover:
            cover_html = (
                f'<img class="cover" src="{safe_url(cover["url"] or cover["source_url"])}" '
                'alt="Обложка новости" loading="lazy" referrerpolicy="no-referrer">'
            )
        date = article["source_published_at"]
        if date:
            date = datetime.fromisoformat(date.replace("Z", "+00:00"))
            date = date.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y")
        else:
            date = "Дата не указана"
        body = "".join(f"<p>{escape(p)}</p>" for p in article["body"].split("\n\n") if p.strip())
        image_list = "".join(
            f'<li><a href="{safe_url(im["url"] or im["source_url"])}" target="_blank" '
            f'rel="noopener noreferrer">Изображение {index + 1}</a> '
            f"— {'сохранено в file_service' if im['file_id'] else 'не загружено в file_service'}</li>"
            for index, im in enumerate(pictures)
        )
        cards.append(
            f'<article>{cover_html}<div class="content"><div class="date">ФСП РОССИИ · {date}</div>'
            f"<h2>{escape(article['title'])}</h2>"
            f'<p class="excerpt">{escape(article["body"][:240])}…</p>'
            f'<details><summary>Прочитать полный текст</summary><div class="body">{body}</div>'
            f"<h3>Изображения</h3><ul>{image_list}</ul></details>"
            f'<footer><a href="{safe_url(article["canonical_url"])}" target="_blank" '
            'rel="noopener noreferrer">Оригинал на сайте ФСП ↗</a>'
            f"<span>ID: {escape(article['id'])}</span></footer></div></article>"
        )
    created = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M МСК")
    return """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Результаты парсинга · News Parser</title>
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#1e293b;background:#f3f5f9}
*{box-sizing:border-box}body{margin:0}main{max-width:1050px;margin:auto;padding:40px 22px 80px}
.eyebrow{font-size:12px;letter-spacing:2px;color:#465ac6;font-weight:750}h1{font-size:36px;margin:12px 0}
.intro{color:#536178;line-height:1.65;max-width:760px}.stats{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0}
.stat{background:white;padding:18px 24px;border:1px solid #e1e6ef;border-radius:12px;min-width:170px}
.stat strong{display:block;font-size:27px}.stat span{font-size:13px;color:#536178}
.notice{background:#fff8e6;border:1px solid #f0d897;border-radius:12px;padding:16px 20px;line-height:1.65}
nav{display:flex;gap:20px;margin:22px 0}a{color:#344fc0;text-underline-offset:3px}
input{display:block;width:100%;padding:14px 16px;border:1px solid #cbd3e0;border-radius:10px;
font:inherit;background:white;margin:26px 0}article{background:white;border:1px solid #dfe5ef;
border-radius:16px;margin:24px 0;overflow:hidden;box-shadow:0 4px 16px #14274a06}
.cover{width:100%;max-height:350px;object-fit:contain;background:#e9edf4;display:block}
.content{padding:26px 30px}.date{font-size:12px;color:#6b7890;letter-spacing:.8px}
h2{font-size:24px;line-height:1.35;margin:12px 0 16px}.excerpt{color:#536178;line-height:1.7}
summary{cursor:pointer;font-weight:650;color:#344fc0;padding:12px 0}.body{line-height:1.8;overflow-wrap:anywhere}
li{margin:8px 0}footer{border-top:1px solid #e7ebf2;margin-top:22px;padding-top:18px;
display:flex;gap:16px;flex-wrap:wrap;justify-content:space-between;font-size:12px}
footer span{color:#718096;overflow-wrap:anywhere}.muted{font-size:13px;color:#64748b}article[hidden]{display:none}
@media(max-width:600px){h1{font-size:28px}.content{padding:20px}h2{font-size:21px}}
</style><main><div class="eyebrow">NEWS PARSER / ЛОКАЛЬНЫЙ ТЕСТ</div>
<h1>Новости уже в базе</h1><p class="intro">Результаты реального задания парсера:
заголовки, тексты, даты и ссылки на изображения получены с сайта ФСП, сохранены в SQLite
и прочитаны через API news_parser. Это снимок результатов на момент формирования отчёта.</p>
""" + (
        f'<div class="stats"><div class="stat"><strong>{len(articles)}</strong><span>новостей в отчёте</span></div>'
        f'<div class="stat"><strong>{image_count}</strong><span>изображений обнаружено</span></div>'
        f'<div class="stat"><strong>{stored}</strong><span>сохранено в file_service</span></div></div>'
        f'<div class="notice"><strong>Хранилище изображений.</strong> {storage_notice}</div>'
        '<nav><a href="http://127.0.0.1:8002/docs" target="_blank" rel="noopener">Swagger API ↗</a>'
        '<a href="results.json" target="_blank" rel="noopener">Открыть JSON результатов</a></nav>'
        f'<p class="muted">Сформирован: {created} · Задание: {escape(job["id"])} · '
        f"Статус: {escape(job['status'])} (partial означает частичный успех)</p>"
        '<label for="search">Поиск по заголовкам и текстам</label><input id="search" '
        'type="search" placeholder="Начните вводить название или слово из новости…">'
        + "".join(cards)
        + '<p id="empty" hidden>Совпадений не найдено.</p></main><script>'
        'document.querySelector("#search").addEventListener("input",event=>{'
        "let count=0;const query=event.target.value.toLocaleLowerCase();"
        'document.querySelectorAll("article").forEach(card=>{'
        "card.hidden=!card.textContent.toLocaleLowerCase().includes(query);if(!card.hidden)count++;});"
        'document.querySelector("#empty").hidden=count!==0;});</script></html>'
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--output", default=".scratch/preview")
    args = parser.parse_args()
    settings = get_settings()
    with httpx.Client(
        base_url="http://127.0.0.1:8002",
        trust_env=False,
        timeout=20,
        headers={"X-API-Key": settings.api_key},
    ) as client:
        response = client.get(f"/api/jobs/{args.job_id}")
        response.raise_for_status()
        job = response.json()
        if job["status"] in {"queued", "running"}:
            raise SystemExit("Job has not finished yet")
        articles = []
        for article_id in job["result"].get("article_ids", []):
            response = client.get(f"/api/articles/{article_id}")
            response.raise_for_status()
            articles.append(response.json())
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(build_report(articles, job), encoding="utf-8")
    (output / "results.json").write_text(
        json.dumps({"job": job, "articles": articles}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"articles": len(articles), "status": job["status"], "report": str(output)}))


if __name__ == "__main__":
    main()
