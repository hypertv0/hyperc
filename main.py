import asyncio
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
import re
import time
import random
import sys
import json

# --- AYARLAR ---
BASE_URL = "https://cizgimax.online"
OUTPUT_FILE = "cizgimax.m3u"
CONCURRENT_LIMIT = 3   # Biraz artırdık çünkü artık boşuna beklemeyeceğiz
MAX_PAGES = 3          # Tarama derinliği
TIMEOUT = 30

# Kategoriler
CATEGORIES = {
    "Son Eklenenler": f"{BASE_URL}/diziler/?orderby=date&order=DESC",
    "Aile": f"{BASE_URL}/diziler/?s_type&tur[0]=aile&orderby=date&order=DESC",
    "Aksiyon": f"{BASE_URL}/diziler/?s_type&tur[0]=aksiyon-macera&orderby=date&order=DESC",
    "Animasyon": f"{BASE_URL}/diziler/?s_type&tur[0]=animasyon&orderby=date&order=DESC",
    "Bilim Kurgu": f"{BASE_URL}/diziler/?s_type&tur[0]=bilim-kurgu-fantazi&orderby=date&order=DESC",
    "Çocuklar": f"{BASE_URL}/diziler/?s_type&tur[0]=cocuklar&orderby=date&order=DESC",
    "Komedi": f"{BASE_URL}/diziler/?s_type&tur[0]=komedi&orderby=date&order=DESC"
}

# Headerlar
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Referer": BASE_URL,
}

FINAL_PLAYLIST = []

async def random_sleep(min_t=0.5, max_t=1.5):
    await asyncio.sleep(random.uniform(min_t, max_t))

async def fetch_text(session, url, referer=None):
    headers = HEADERS.copy()
    if referer: headers["Referer"] = referer
    
    try:
        response = await session.get(url, headers=headers, timeout=TIMEOUT, impersonate="chrome120")
        if response.status_code == 200:
            return response.text
        elif response.status_code == 403:
            # 403 alırsak sessizce geç, log kirliliği yapma (CizgiPass vb.)
            return None
        elif response.status_code == 400 and "sibnet" in url:
            print(f"Sibnet 400 Hatası: {url} (Referer veya ID sorunu)")
            return None
    except Exception:
        pass
    return None

async def resolve_video_source(session, iframe_url, original_page_url):
    """
    iframe url'sinden video linkini çözer.
    """
    if not iframe_url: return None

    # URL Düzeltme
    if iframe_url.startswith("//"): iframe_url = "https:" + iframe_url

    # --- SIBNET ÇÖZÜCÜ (ÖZEL DÜZELTME) ---
    # Loglardaki hata: shell.php?videoid=... 400 Bad Request veriyor.
    # Çözüm: Bunu video sayfasına çevirip istek atacağız.
    if "sibnet.ru" in iframe_url:
        video_id = None
        # ID'yi URL'den çekmeye çalış
        if "videoid=" in iframe_url:
            match = re.search(r'videoid=(\d+)', iframe_url)
            if match: video_id = match.group(1)
        elif "/video/" in iframe_url:
            match = re.search(r'/video/(\d+)', iframe_url)
            if match: video_id = match.group(1)
            
        if video_id:
            # Sibnet shell.php yerine doğrudan video sayfasına git
            sibnet_page_url = f"https://video.sibnet.ru/video/{video_id}"
            
            # Sibnet Referer Kontrolü Yapar: Referer CizgiMax olmalı
            text = await fetch_text(session, sibnet_page_url, referer=original_page_url)
            
            if text:
                # Video linkini regex ile bul
                slug_match = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', text)
                if slug_match:
                    slug = slug_match.group(1)
                    if slug.startswith("/"):
                        return f"https://video.sibnet.ru{slug}"
                    return slug
        else:
            # ID bulamazsak direkt dene (muhtemelen çalışmaz ama)
            pass

    # --- DİĞER KAYNAKLAR (M3U8) ---
    # CizgiPass/Duo 403 verdiği için onları pas geçiyoruz veya şansımızı deniyoruz.
    if "cizgipass" in iframe_url or "cizgiduo" in iframe_url:
        # Bu kaynaklar GitHub IP'sini engelliyor, boşuna deneme yapıp zaman kaybetmeyelim.
        return None

    # Diğer genel kaynaklar için
    text = await fetch_text(session, iframe_url, referer=original_page_url)
    if text:
        m3u8_match = re.search(r'(https?://[^"\']+\.m3u8)', text)
        if m3u8_match: return m3u8_match.group(1)
        
        mp4_match = re.search(r'(https?://[^"\']+\.mp4)', text)
        if mp4_match: return mp4_match.group(1)

    return None

async def process_episode(session, category, series_title, ep_title, ep_url, poster):
    """Bölüm sayfasına girer ve videoyu bulur."""
    # Bölüm sayfasına git
    html = await fetch_text(session, ep_url, referer=BASE_URL)
    if not html: return

    soup = BeautifulSoup(html, 'html.parser')
    links = soup.select("ul.linkler li a")
    
    found_url = None
    
    # Sibnet linklerini en başa al, CizgiPass'leri en sona at
    sorted_links = sorted(links, key=lambda x: (
        1 if "sibnet" in (x.get("data-frame") or "") else 
        0 if "cizgi" not in (x.get("data-frame") or "") else -1
    ), reverse=True)

    for link in sorted_links:
        iframe_src = link.get("data-frame")
        if not iframe_src: continue
        
        # Videoyu çöz
        video_url = await resolve_video_source(session, iframe_src, ep_url)
        
        if video_url:
            found_url = video_url
            break 
    
    if found_url:
        clean_ep = ep_title.replace(series_title, "").strip()
        if not clean_ep: clean_ep = ep_title
        
        full_title = f"{series_title} - {clean_ep}"
        
        FINAL_PLAYLIST.append({
            "group": category,
            "title": full_title,
            "logo": poster,
            "url": found_url
        })
        print(f"  [+] Eklendi: {full_title}")
    else:
        # Sadece hata logunu bas, içerik yoksa yapacak bir şey yok (IP Ban)
        pass

async def process_series(session, semaphore, category, series_title, series_url, poster):
    async with semaphore:
        html = await fetch_text(session, series_url)
        if not html: return

        soup = BeautifulSoup(html, 'html.parser')
        episodes_divs = soup.select("div.asisotope div.ajax_post")
        
        tasks = []
        for ep_div in episodes_divs:
            name_span = ep_div.select_one("span.episode-names")
            if not name_span: continue
            ep_name = name_span.text.strip()
            
            link_tag = ep_div.select_one("a")
            if not link_tag: continue
            ep_link = link_tag['href']
            
            tasks.append(process_episode(session, category, series_title, ep_name, ep_link, poster))
            
        if tasks:
            print(f" > Dizi: {series_title} ({len(tasks)} Bölüm)")
            # 10'lu gruplar halinde işle (Hızlandırıldı)
            chunk_size = 10
            for i in range(0, len(tasks), chunk_size):
                chunk = tasks[i:i + chunk_size]
                await asyncio.gather(*chunk)

async def scan_category(session, semaphore, cat_name, cat_url):
    print(f"\n--- Kategori: {cat_name} ---")
    
    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = cat_url
        else:
            if "/?p=" in cat_url:
                parts = cat_url.split("/?p=")
                url = f"{parts[0]}/page/{page}/?p={parts[1]}"
            else:
                parts = cat_url.split("/?")
                base = parts[0]
                query = "/?" + parts[1] if len(parts) > 1 else ""
                if base.endswith("/"): base = base[:-1]
                url = f"{base}/page/{page}{query}"

        print(f" >> Sayfa {page}...")
        html = await fetch_text(session, url)
        if not html: break
        
        soup = BeautifulSoup(html, 'html.parser')
        items = soup.select("ul.filter-results li")
        
        if not items: break
            
        tasks = []
        for item in items:
            title_tag = item.select_one("h2.truncate")
            if not title_tag: continue
            title = title_tag.text.strip()
            
            link_tag = item.select_one("div.poster-subject a")
            if not link_tag: continue
            link = link_tag['href']
            
            img_tag = item.select_one("div.poster-media img")
            poster = img_tag.get("data-src") if img_tag else ""
            
            tasks.append(process_series(session, semaphore, category=cat_name, series_title=title, series_url=link, poster=poster))
        
        await asyncio.gather(*tasks)

async def main():
    print("CizgiMax Sibnet Fix Bot...")
    start_time = time.time()
    
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    async with AsyncSession() as session:
        for c_name, c_url in CATEGORIES.items():
            await scan_category(session, semaphore, c_name, c_url)
    
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik. Kaydediliyor...")
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
