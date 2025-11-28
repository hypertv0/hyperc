import asyncio
from curl_cffi.requests import AsyncSession # Cloudflare ve korumaları aşmak için
from bs4 import BeautifulSoup
import re
import time
import json
import sys

# --- AYARLAR ---
BASE_URL = "https://cizgimax.online"
OUTPUT_FILE = "cizgimax.m3u"
CONCURRENT_LIMIT = 5   # Aynı anda kaç işlem yapılsın? (Çok artırma, site engeller)
MAX_PAGES = 3          # Her kategoriden kaç sayfa taransın? (Hepsini isterseniz 50 yapın)
TIMEOUT = 30

# Kategoriler (Kotlin kodundaki mainPageOf kısmından alındı)
CATEGORIES = {
    "Son Eklenenler": f"{BASE_URL}/diziler/?orderby=date&order=DESC",
    "Aile": f"{BASE_URL}/diziler/?s_type&tur[0]=aile&orderby=date&order=DESC",
    "Aksiyon": f"{BASE_URL}/diziler/?s_type&tur[0]=aksiyon-macera&orderby=date&order=DESC",
    "Animasyon": f"{BASE_URL}/diziler/?s_type&tur[0]=animasyon&orderby=date&order=DESC",
    "Bilim Kurgu": f"{BASE_URL}/diziler/?s_type&tur[0]=bilim-kurgu-fantazi&orderby=date&order=DESC",
    "Çocuklar": f"{BASE_URL}/diziler/?s_type&tur[0]=cocuklar&orderby=date&order=DESC",
    "Komedi": f"{BASE_URL}/diziler/?s_type&tur[0]=komedi&orderby=date&order=DESC"
}

# Tarayıcı Taklidi Yapan Headerlar
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": BASE_URL,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"
}

FINAL_PLAYLIST = []

async def fetch_text(session, url, referer=None):
    """Url içeriğini çeker (curl_cffi kullanarak)"""
    headers = HEADERS.copy()
    if referer:
        headers["Referer"] = referer
    
    try:
        # impersonate="chrome" sayesinde Cloudflare'i kandırır
        response = await session.get(url, headers=headers, timeout=TIMEOUT, impersonate="chrome120")
        if response.status_code == 200:
            return response.text
        else:
            print(f"Hata ({response.status_code}): {url}")
    except Exception as e:
        print(f"Bağlantı Hatası: {url} -> {e}")
    return None

async def resolve_video_source(session, iframe_url):
    """
    iframe url'sine gider ve gerçek video linkini (mp4/m3u8) bulmaya çalışır.
    Sibnet ve Genel M3U8 taraması yapar.
    """
    if not iframe_url: return None
    
    # 1. Kaynak: Sibnet
    if "sibnet.ru" in iframe_url:
        text = await fetch_text(session, iframe_url)
        if text:
            # Kotlin: player.src([{src: "..."
            match = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', text)
            if match:
                video_slug = match.group(1)
                return f"https://video.sibnet.ru{video_slug}"
    
    # 2. Kaynak: CizgiDuo veya Diğerleri
    # iframe sayfasına git
    text = await fetch_text(session, iframe_url, referer=BASE_URL)
    if not text: return None

    # a) Direkt .m3u8 var mı?
    m3u8_match = re.search(r'(https?://[^"\']+\.m3u8)', text)
    if m3u8_match:
        return m3u8_match.group(1)
    
    # b) Kotlin kodundaki 'video_location' regexi (CizgiDuo şifresi çözülmüş json içinde olabilir)
    # Şifrelemeyi kırmak zor olduğu için text içinde raw arama yapıyoruz.
    loc_match = re.search(r'video_location":"([^"]+)"', text)
    if loc_match:
        return loc_match.group(1).replace("\\", "")

    return None

async def process_episode(session, category, series_title, ep_title, ep_url, poster):
    """Bölüm sayfasına girer, iframe'i bulur ve videoyu çözer."""
    html = await fetch_text(session, ep_url)
    if not html: return

    soup = BeautifulSoup(html, 'html.parser')
    
    # iframe linklerini bul (ul.linkler li a[data-frame])
    # Kotlin: document.select("ul.linkler li")... attr("data-frame")
    links_ul = soup.select("ul.linkler li a")
    
    found_url = None
    
    # Linkleri sırayla dene (Önce Sibnet'i tercih et çünkü daha stabil)
    sorted_links = sorted(links_ul, key=lambda x: "sibnet" not in (x.get("data-frame") or ""), reverse=True)

    for link in sorted_links:
        iframe_src = link.get("data-frame")
        if not iframe_src: continue
        
        # URL'yi düzelt (bazen // ile başlar)
        if iframe_src.startswith("//"):
            iframe_src = "https:" + iframe_src
            
        video_url = await resolve_video_source(session, iframe_src)
        if video_url:
            found_url = video_url
            break # Videoyu bulduk, diğer kaynaklara bakmaya gerek yok
    
    if found_url:
        # Bölüm ismini temizle
        clean_ep_title = ep_title.replace(series_title, "").strip()
        if not clean_ep_title: clean_ep_title = ep_title
        
        # M3U Formatına uygun başlık: Dizi Adı - Sezon X Bölüm Y
        full_title = f"{series_title} - {clean_ep_title}"
        
        FINAL_PLAYLIST.append({
            "group": category,
            "title": full_title,
            "logo": poster,
            "url": found_url
        })
        print(f"  [+] Eklendi: {full_title}")
    else:
        print(f"  [-] Video Bulunamadı: {series_title} - {ep_title}")

async def process_series(session, semaphore, category, series_title, series_url, poster):
    """Dizi sayfasına girer ve bölümleri listeler."""
    async with semaphore:
        html = await fetch_text(session, series_url)
        if not html: return

        soup = BeautifulSoup(html, 'html.parser')
        
        # Bölümleri bul: div.asisotope div.ajax_post
        episodes_divs = soup.select("div.asisotope div.ajax_post")
        
        tasks = []
        for ep_div in episodes_divs:
            # Bölüm Adı: span.episode-names
            name_span = ep_div.select_one("span.episode-names")
            if not name_span: continue
            ep_name = name_span.text.strip()
            
            # Bölüm Linki
            link_tag = ep_div.select_one("a")
            if not link_tag: continue
            ep_link = link_tag['href']
            
            # Sezon/Bölüm bilgisini Kotlin'deki regex ile alabiliriz ama 
            # başlık zaten yeterince açıklayıcı genellikle.
            
            # Her bölümü işlemek için görev oluştur
            # Not: Bölüm içine girip video çekmek uzun sürer.
            # Eğer çok yavaş olursa sadece ilk bölümü çekmek gibi optimizasyonlar yapılabilir.
            # Şimdilik hepsini çekiyoruz.
            tasks.append(process_episode(session, category, series_title, ep_name, ep_link, poster))
            
        if tasks:
            print(f" > Dizi Taranıyor: {series_title} ({len(tasks)} Bölüm)")
            await asyncio.gather(*tasks)

async def scan_category(session, semaphore, cat_name, cat_url):
    """Kategorinin sayfalarını gezer."""
    print(f"\n--- Kategori Başladı: {cat_name} ---")
    
    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = cat_url
        else:
            # Sayfalama yapısı: /diziler/page/2/?...
            # URL manipülasyonu
            parts = cat_url.split("/?p=") if "/?p=" in cat_url else cat_url.split("/?")
            base = parts[0]
            query = "/?" + parts[1] if len(parts) > 1 else ""
            
            # Eğer base sonunda /diziler varsa
            if base.endswith("/"): base = base[:-1]
            url = f"{base}/page/{page}{query}"

        print(f" >> {cat_name} Sayfa {page} taranıyor...")
        html = await fetch_text(session, url)
        if not html: break
        
        soup = BeautifulSoup(html, 'html.parser')
        
        # Liste Elemanları: ul.filter-results li
        items = soup.select("ul.filter-results li")
        if not items:
            print("   -> İçerik bitti.")
            break
            
        tasks = []
        for item in items:
            # Başlık: h2.truncate
            title_tag = item.select_one("h2.truncate")
            if not title_tag: continue
            title = title_tag.text.strip()
            
            # Link: div.poster-subject a
            link_tag = item.select_one("div.poster-subject a")
            if not link_tag: continue
            link = link_tag['href']
            
            # Poster: div.poster-media img (data-src)
            img_tag = item.select_one("div.poster-media img")
            poster = img_tag.get("data-src") if img_tag else ""
            
            tasks.append(process_series(session, semaphore, cat_name, title, link, poster))
        
        await asyncio.gather(*tasks)

async def main():
    print("CizgiMax Bot Başlatılıyor...")
    start_time = time.time()
    
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    async with AsyncSession() as session:
        cat_tasks = []
        for c_name, c_url in CATEGORIES.items():
            cat_tasks.append(scan_category(session, semaphore, c_name, c_url))
        
        await asyncio.gather(*cat_tasks)
        
    print(f"\nToplam {len(FINAL_PLAYLIST)} video bulundu. Dosya yazılıyor...")
    
    # Sıralama: Kategori -> Dizi Adı -> Bölüm Adı (Doğal Sıralama için basit sort)
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            # BelgeselX uyumlu format
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

    print(f"İşlem Tamamlandı! Süre: {time.time() - start_time:.2f}sn")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
