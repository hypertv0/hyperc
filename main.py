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
CONCURRENT_LIMIT = 2   # DÜŞÜRÜLDÜ: Engellenmemek için aynı anda max 2 işlem
MAX_PAGES = 2          # Test için 2 sayfa (Çalışırsa artırabilirsiniz)
TIMEOUT = 45           # Zaman aşımı artırıldı

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

# Standart Tarayıcı Headerları
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": BASE_URL,
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1"
}

FINAL_PLAYLIST = []

async def random_sleep(min_t=1.0, max_t=3.0):
    """Robot olmadığımızı kanıtlamak için rastgele bekleme."""
    await asyncio.sleep(random.uniform(min_t, max_t))

async def fetch_text(session, url, referer=None, retries=3):
    """Güçlendirilmiş Fetch Fonksiyonu"""
    headers = HEADERS.copy()
    if referer:
        headers["Referer"] = referer
    
    for i in range(retries):
        try:
            # impersonate="chrome110" genellikle Cloudflare tarafından daha az şüphe çeker
            response = await session.get(url, headers=headers, timeout=TIMEOUT, impersonate="chrome110")
            
            if response.status_code == 200:
                return response.text
            elif response.status_code == 403:
                print(f"!!! 403 BLOKLANDI (Deneme {i+1}): {url}")
                await random_sleep(5, 10) # Uzun bekle
            elif response.status_code == 404:
                return None
            else:
                print(f"Hata Kodu {response.status_code}: {url}")
                
        except Exception as e:
            print(f"Bağlantı Hatası: {str(e)}")
            await random_sleep(2, 4)
            
    return None

async def resolve_video_source(session, iframe_url, original_page_url):
    """
    iframe url'sinden video linkini çözer.
    """
    if not iframe_url: return None

    # URL // ile başlıyorsa düzelt
    if iframe_url.startswith("//"):
        iframe_url = "https:" + iframe_url

    # --- SIBNET ÇÖZÜCÜ ---
    if "sibnet.ru" in iframe_url:
        # Sibnet'e istek atarken Referer mutlaka dizinin izleme sayfası olmalı!
        text = await fetch_text(session, iframe_url, referer=original_page_url)
        if text:
            # Regex: player.src([{src: "/v/..."
            # Bazen video slug /v/ ile başlar, bazen tam url olur.
            match = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', text)
            if match:
                slug = match.group(1)
                if slug.startswith("/"):
                    return f"https://video.sibnet.ru{slug}"
                return f"https://video.sibnet.ru/{slug}" if not slug.startswith("http") else slug

    # --- CIZGIDUO / M3U8 ÇÖZÜCÜ ---
    # Eğer Sibnet değilse veya başarısızsa iframe'in içine bak
    text = await fetch_text(session, iframe_url, referer=original_page_url)
    if not text: return None

    # 1. Açıkta duran .m3u8 var mı?
    m3u8_match = re.search(r'(https?://[^"\']+\.m3u8)', text)
    if m3u8_match:
        return m3u8_match.group(1)
    
    # 2. MP4 var mı?
    mp4_match = re.search(r'(https?://[^"\']+\.mp4)', text)
    if mp4_match:
        return mp4_match.group(1)

    return None

async def process_episode(session, category, series_title, ep_title, ep_url, poster):
    """Bölüm sayfasına girer ve videoyu bulur."""
    await random_sleep(0.5, 1.5) # Hızlı istek atmamak için bekle
    
    html = await fetch_text(session, ep_url, referer=BASE_URL)
    if not html: return

    soup = BeautifulSoup(html, 'html.parser')
    
    # Tüm iframe kaynaklarını topla
    # CizgiMax yapısında genelde: ul.linkler li a[data-frame]
    links = soup.select("ul.linkler li a")
    
    found_url = None
    
    # Kaynakları sırayla dene. Öncelik Sibnet.
    # Sibnet linklerini başa al
    sorted_links = sorted(links, key=lambda x: "sibnet" in (x.get("data-frame") or ""), reverse=True)

    for link in sorted_links:
        iframe_src = link.get("data-frame")
        if not iframe_src: continue
        
        # Video linkini çözmeye çalış
        video_url = await resolve_video_source(session, iframe_src, ep_url)
        
        if video_url:
            found_url = video_url
            break 
    
    if found_url:
        # Başlığı temizle: "Dizi Adı - Bölüm Adı" formatı
        clean_ep = ep_title.replace(series_title, "").strip()
        if not clean_ep: clean_ep = ep_title
        
        # M3U Uyumlu Başlık
        # "Dizi Adı - Bölüm..." formatı HTML tarafında gruplama için kritiktir.
        full_title = f"{series_title} - {clean_ep}"
        
        FINAL_PLAYLIST.append({
            "group": category,
            "title": full_title,
            "logo": poster,
            "url": found_url
        })
        print(f"  [+] Eklendi: {full_title}")
    else:
        print(f"  [-] Video Çözülemedi: {ep_title}")

async def process_series(session, semaphore, category, series_title, series_url, poster):
    """Dizi sayfasındaki tüm bölümleri tarar."""
    async with semaphore:
        await random_sleep(0.5, 2.0) # Her diziye girişte bekle
        
        html = await fetch_text(session, series_url)
        if not html: return

        soup = BeautifulSoup(html, 'html.parser')
        
        # Bölümleri bul
        episodes_divs = soup.select("div.asisotope div.ajax_post")
        
        # Bölümleri tersten işle (Eskiden yeniye veya tam tersi, site yapısına göre)
        # Genelde site yeni bölümleri üste koyar.
        
        tasks = []
        for ep_div in episodes_divs:
            name_span = ep_div.select_one("span.episode-names")
            if not name_span: continue
            
            ep_name = name_span.text.strip()
            
            link_tag = ep_div.select_one("a")
            if not link_tag: continue
            ep_link = link_tag['href']
            
            # Her bölüm için task oluştur
            tasks.append(process_episode(session, category, series_title, ep_name, ep_link, poster))
            
        if tasks:
            print(f" > Dizi İşleniyor: {series_title} ({len(tasks)} Bölüm)")
            # Hepsini aynı anda başlatma, yavaş yavaş yap
            # Chunking: 5'erli gruplar halinde işle
            chunk_size = 5
            for i in range(0, len(tasks), chunk_size):
                chunk = tasks[i:i + chunk_size]
                await asyncio.gather(*chunk)
                await random_sleep(1, 3) # Gruplar arası dinlen

async def scan_category(session, semaphore, cat_name, cat_url):
    print(f"\n--- Kategori: {cat_name} ---")
    
    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = cat_url
        else:
            # URL yapısını koruyarak sayfa numarasını ekle
            if "/?p=" in cat_url: # Arama sonucu vb.
                parts = cat_url.split("/?p=")
                url = f"{parts[0]}/page/{page}/?p={parts[1]}"
            else:
                parts = cat_url.split("/?")
                base = parts[0]
                query = "/?" + parts[1] if len(parts) > 1 else ""
                if base.endswith("/"): base = base[:-1]
                url = f"{base}/page/{page}{query}"

        print(f" >> {cat_name} Sayfa {page}...")
        html = await fetch_text(session, url)
        
        if not html: break
        
        soup = BeautifulSoup(html, 'html.parser')
        items = soup.select("ul.filter-results li")
        
        if not items:
            print("   -> İçerik bitti.")
            break
            
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
            
            tasks.append(process_series(session, semaphore, cat_name, title, link, poster))
        
        await asyncio.gather(*tasks)

async def main():
    print("CizgiMax Anti-Block Bot v2...")
    start_time = time.time()
    
    # Semaphore 2: Çok yavaş ama güvenli. 403 yememek için.
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    async with AsyncSession() as session:
        # Kategorileri sırayla tara (Paralel değil)
        # Çünkü Cloudflare paralel istekleri saldırı sanıyor.
        for c_name, c_url in CATEGORIES.items():
            await scan_category(session, semaphore, c_name, c_url)
            await random_sleep(5, 10) # Kategoriler arası uzun mola
        
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik bulundu. M3U oluşturuluyor...")
    
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

    print(f"Bitti! Süre: {time.time() - start_time:.2f}sn")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
