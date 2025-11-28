import asyncio
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
import re
import time
import base64
import sys
import random
import xml.etree.ElementTree as ET
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# --- AYARLAR ---
BASE_URL = "https://cizgimax.online"
OUTPUT_FILE = "cizgimax.m3u"
CONCURRENT_LIMIT = 20   # 20 İdeal hız
TIMEOUT = 15            # 15 Saniye zaman aşımı

# SITEMAP LİSTESİ (Siteden alınan veriye göre elle tanımlandı - Anti-Block için)
# Ana sitemap'e girmeden direkt bu adreslere saldıracağız.
TARGET_SITEMAPS = [
    "https://cizgimax.online/post-sitemap.xml",  # En güncel olan
]
# Geriye kalan 2'den 33'e kadar olanları otomatik ekle
for i in range(2, 34):
    TARGET_SITEMAPS.append(f"https://cizgimax.online/post-sitemap{i}.xml")

# Ekstra Dizi sitemapleri
TARGET_SITEMAPS.append("https://cizgimax.online/diziler-sitemap.xml")

FINAL_PLAYLIST = []
URL_QUEUE = asyncio.Queue()
PROCESSED = 0
TOTAL_URLS = 0

# --- ŞİFRE ÇÖZME ---
def decrypt_aes(encrypted, password):
    try:
        key = password.encode('utf-8')
        if len(key) > 32: key = key[:32]
        elif len(key) < 32: key = key.ljust(32, b'\0')
        iv = key[:16]
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        try: dec = unpad(cipher.decrypt(base64.b64decode(encrypted)), AES.block_size)
        except: dec = cipher.decrypt(base64.b64decode(encrypted))
        return dec.decode('utf-8', 'ignore').strip().replace('\x00', '')
    except: return None

# --- AĞ İSTEKLERİ ---
async def fetch_text(session, url, referer=None):
    headers = {
        "Referer": referer if referer else BASE_URL,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        # impersonate="chrome120" -> Cloudflare bypass
        response = await session.get(url, headers=headers, timeout=TIMEOUT, impersonate="chrome120")
        if response.status_code == 200:
            return response.text
    except:
        pass
    return None

# --- VİDEO ÇÖZÜCÜ ---
async def resolve_video(session, iframe_url, referer_url):
    if not iframe_url: return None
    if iframe_url.startswith("//"): iframe_url = "https:" + iframe_url

    # 1. CizgiDuo / CizgiPass / CizgiMax Player
    if "cizgi" in iframe_url:
        html = await fetch_text(session, iframe_url, referer=referer_url)
        if html:
            # bePlayer şifreli veri
            m = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
            if m:
                data_match = re.search(r'"data"\s*:\s*"([^"]+)"', m.group(2))
                if data_match:
                    dec = decrypt_aes(data_match.group(1), m.group(1))
                    if dec:
                        res = re.search(r'video_location":"([^"]+)"', dec)
                        if res: return res.group(1).replace("\\", "")

    # 2. Sibnet
    elif "sibnet.ru" in iframe_url:
        if "|" in iframe_url: iframe_url = iframe_url.split("|")[0]
        vid = re.search(r'(?:videoid=|/video/)(\d+)', iframe_url)
        if vid:
            real = f"https://video.sibnet.ru/video/{vid.group(1)}"
            html = await fetch_text(session, real, referer=referer_url)
            if html:
                slug = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html)
                if slug:
                    s = slug.group(1)
                    return f"https://video.sibnet.ru{s}" if s.startswith("/") else s

    # 3. Genel Tarama (Yedek)
    else:
        html = await fetch_text(session, iframe_url, referer=referer_url)
        if html:
            m3u = re.search(r'(https?://[^"\'\s]+\.m3u8)', html)
            if m3u: return m3u.group(1)
            mp4 = re.search(r'(https?://[^"\'\s]+\.mp4)', html)
            if mp4: return mp4.group(1)

    return None

# --- WORKER (İşçi) ---
async def worker(worker_id, session):
    global PROCESSED
    while True:
        try:
            page_url = await URL_QUEUE.get()
        except asyncio.QueueEmpty:
            break

        html = await fetch_text(session, page_url)
        if html:
            soup = BeautifulSoup(html, 'html.parser')
            
            # Başlık (HTML çıktısından analiz edildi)
            # <h1 class="page-title">...<span class="light-title">...</span></h1>
            h1 = soup.select_one("h1.page-title")
            
            full_title = "Bilinmeyen"
            category = "Genel"

            if h1:
                # Dizi Adı
                a_tag = h1.select_one("a")
                series_name = a_tag.text.strip() if a_tag else ""
                
                # Bölüm Adı
                span_tag = h1.select_one("span.light-title")
                episode_name = span_tag.text.strip() if span_tag else ""
                
                full_title = f"{series_name} - {episode_name}"
            
            # Kategori: Breadcrumb içinden (HTML çıktısında 2. item genellikle kategori/dizi adı olur)
            # <div class="Breadcrumb"> ... item ... item ...
            breadcrumb = soup.select("div.Breadcrumb span[itemprop='name']")
            if breadcrumb and len(breadcrumb) > 1:
                # Genellikle 2. eleman Dizi Adıdır, Kategoriyi sitemap yapısından çıkaramıyoruz
                # Bu yüzden genel bir kategori veya dizi adını kullanabiliriz.
                # Ancak HTML içinde <a href=".../tur/aksiyon"> gibi linkler var mı?
                genre_links = soup.select("ul.sub-menu li a[href*='/tur/']")
                if not genre_links:
                     # Menüdeki türleri değil içerik türünü arayalım
                     # Genelde <div class="poster-meta"> içinde olur ama bölüm sayfasında olmayabilir.
                     # Dizi adını kategori olarak kullanalım (En güvenlisi)
                     category = series_name
            
            # Poster
            # Bölüm sayfasında poster olmayabilir, ana diziye gitmek gerekir.
            # Hız için varsayılan logo veya boş geçiyoruz.
            poster = "" 

            # Video Linkleri (HTML: <ul class="linkler"> <a data-frame="...">)
            links = soup.select("ul.linkler li a")
            
            # Sıralama
            sorted_links = sorted(links, key=lambda x: (
                2 if "sibnet" in str(x.get("data-frame")) else
                1 if "cizgi" in str(x.get("data-frame")) else 0
            ), reverse=True)

            found_url = None
            for link in sorted_links:
                src = link.get("data-frame")
                found_url = await resolve_video(session, src, page_url)
                if found_url: break
            
            if found_url and "Bilinmeyen" not in full_title:
                FINAL_PLAYLIST.append({
                    "group": category,
                    "title": full_title,
                    "logo": poster,
                    "url": found_url
                })
                
        PROCESSED += 1
        if PROCESSED % 50 == 0:
            print(f"İlerleme: {PROCESSED}/{TOTAL_URLS} bölüm tarandı.", flush=True)
        
        URL_QUEUE.task_done()

# --- SITEMAP YÜKLEYİCİ ---
async def fill_queue(session):
    global TOTAL_URLS
    print("Sitemap Listesi İşleniyor...", flush=True)
    
    for sitemap_url in TARGET_SITEMAPS:
        # Sadece ilk 5 sitemap'i tara (Güncellik ve Hız için)
        # Hepsini taramak istersen bu if bloğunu kaldır.
        if "post-sitemap6.xml" in sitemap_url: 
            break 
            
        print(f" > XML İndiriliyor: {sitemap_url}", flush=True)
        xml_text = await fetch_text(session, sitemap_url)
        
        if not xml_text: 
            print(f"   !!! {sitemap_url} indirilemedi.", flush=True)
            continue

        try:
            # XML Namespace temizliği
            xml_text = re.sub(r' xmlns="[^"]+"', '', xml_text, count=1)
            root = ET.fromstring(xml_text)
            
            count = 0
            for url_tag in root.findall("url"):
                loc = url_tag.find("loc").text
                # Filtre: Sadece bölüm izleme sayfaları
                # Örnek: .../dizi-adi-1-sezon-1-bolum-izle/
                if "-bolum" in loc or "-izle" in loc:
                    URL_QUEUE.put_nowait(loc)
                    count += 1
            
            TOTAL_URLS += count
            print(f"   + {count} link eklendi.", flush=True)
            
        except Exception as e:
            print(f"   XML Hatası: {e}", flush=True)

async def main():
    print("CizgiMax Ultra-Fast Bot Başlatılıyor...", flush=True)
    start_time = time.time()
    
    async with AsyncSession(impersonate="chrome120") as session:
        # 1. Kuyruğu Doldur
        await fill_queue(session)
        
        if TOTAL_URLS == 0:
            print("Hiçbir link bulunamadı! Çıkılıyor.", flush=True)
            return

        print(f"\nToplam {TOTAL_URLS} içerik kuyrukta. İşçiler başlatılıyor...", flush=True)
        
        # 2. İşçileri Başlat
        workers = []
        for i in range(CONCURRENT_LIMIT):
            workers.append(asyncio.create_task(worker(i, session)))
        
        # Kuyruk bitene kadar bekle
        await URL_QUEUE.join()
        
        # İşçileri durdur
        for w in workers: w.cancel()
    
    # 3. Kaydet
    print(f"\nSonuç: {len(FINAL_PLAYLIST)} içerik bulundu. M3U yazılıyor...", flush=True)
    
    # Sıralama: Dizi Adı
    FINAL_PLAYLIST.sort(key=lambda x: x["title"])
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

    print(f"Toplam Süre: {time.time() - start_time:.2f}sn")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt: pass
