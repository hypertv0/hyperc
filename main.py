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
SITEMAP_INDEX = "https://cizgimax.online/sitemap.xml"
OUTPUT_FILE = "cizgimax.m3u"

# HIZ AYARLARI (DİKKAT: Çok artırma ban yersin)
CONCURRENT_LIMIT = 20   # Aynı anda işlenecek bölüm sayısı
SITEMAP_LIMIT = 5       # Kaç tane alt sitemap taransın? (Her biri 1000 link. 5 = 5000 en güncel bölüm)
TIMEOUT = 20            # İstek zaman aşımı

FINAL_PLAYLIST = []
URL_QUEUE = asyncio.Queue()
PROCESSED = 0
TOTAL_URLS = 0

# --- KOTLIN AES PORTU (CizgiDuo/Pass) ---
def decrypt_aes(encrypted, password):
    try:
        key = password.encode('utf-8')
        if len(key) > 32: key = key[:32]
        elif len(key) < 32: key = key.ljust(32, b'\0')
        iv = key[:16]
        encrypted_bytes = base64.b64decode(encrypted)
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        try: dec = unpad(cipher.decrypt(encrypted_bytes), AES.block_size)
        except: dec = cipher.decrypt(encrypted_bytes)
        return dec.decode('utf-8', 'ignore').strip().replace('\x00', '')
    except: return None

# --- AĞ İSTEKLERİ ---
async def fetch_text(session, url, referer=None):
    # Sitenin HTML kodundaki headerları taklit ediyoruz
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": referer if referer else BASE_URL,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Upgrade-Insecure-Requests": "1"
    }
    try:
        response = await session.get(url, headers=headers, timeout=TIMEOUT, impersonate="chrome120")
        if response.status_code == 200:
            return response.text
        elif response.status_code == 403:
            # print(f"  [!] 403 Erişim Engeli: {url}")
            pass
    except:
        pass
    return None

# --- VİDEO ÇÖZÜCÜ ---
async def resolve_video(session, iframe_url, referer_url):
    if not iframe_url: return None
    if iframe_url.startswith("//"): iframe_url = "https:" + iframe_url

    # 1. CizgiDuo / CizgiPass (Şifreli)
    if "cizgiduo" in iframe_url or "cizgipass" in iframe_url:
        html = await fetch_text(session, iframe_url, referer=referer_url)
        if html:
            # bePlayer('pass', '{data}') regex
            m = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
            if m:
                pwd = m.group(1)
                json_raw = m.group(2)
                data_match = re.search(r'"data"\s*:\s*"([^"]+)"', json_raw)
                if data_match:
                    dec = decrypt_aes(data_match.group(1), pwd)
                    if dec:
                        res = re.search(r'video_location":"([^"]+)"', dec)
                        if res: return res.group(1).replace("\\", "")

    # 2. Sibnet (Link Düzeltme)
    elif "sibnet.ru" in iframe_url:
        # Link temizliği
        if "|" in iframe_url: iframe_url = iframe_url.split("|")[0]
        
        vid = None
        if "videoid=" in iframe_url: vid = re.search(r'videoid=(\d+)', iframe_url)
        elif "/video/" in iframe_url: vid = re.search(r'/video/(\d+)', iframe_url)
        
        if vid:
            # Shell yerine video sayfasına git
            real = f"https://video.sibnet.ru/video/{vid.group(1)}"
            html = await fetch_text(session, real, referer=referer_url)
            if html:
                slug = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html)
                if slug:
                    s = slug.group(1)
                    return f"https://video.sibnet.ru{s}" if s.startswith("/") else s

    # 3. Genel (M3U8/MP4 Avcısı)
    else:
        html = await fetch_text(session, iframe_url, referer=referer_url)
        if html:
            m3u = re.search(r'(https?://[^"\'\s]+\.m3u8)', html)
            if m3u: return m3u.group(1)
            mp4 = re.search(r'(https?://[^"\'\s]+\.mp4)', html)
            if mp4: return mp4.group(1)

    return None

# --- BÖLÜM İŞÇİSİ (WORKER) ---
async def worker(worker_id, session):
    global PROCESSED
    while True:
        try:
            page_url = await URL_QUEUE.get()
        except asyncio.QueueEmpty:
            break

        # Sayfayı İndir
        html = await fetch_text(session, page_url)
        if html:
            soup = BeautifulSoup(html, 'html.parser')
            
            # 1. Başlık ve Kategori
            h1 = soup.select_one("h1.page-title") or soup.select_one("title")
            full_title = h1.text.strip() if h1 else "Bilinmeyen"
            # Temizlik: "Gumball 1. Sezon 1. Bölüm izle" -> "Gumball 1. Sezon 1. Bölüm"
            full_title = full_title.replace("izle", "").replace("ÇizgiMax", "").replace("-", "").strip()

            # Kategori (Breadcrumb veya Etiket)
            # Site yapısında: div.genre-item a
            genres = soup.select("div.genre-item a")
            category = "Genel"
            for g in genres:
                gt = g.text.strip()
                if not gt.isdigit() and len(gt) > 2:
                    category = gt
                    break
            
            # Poster
            img = soup.select_one("img.series-profile-thumb")
            poster = img.get("src") if img else ""

            # 2. Video Linkleri
            links = soup.select("ul.linkler li a")
            
            # Sıralama: Sibnet > Cizgi > Diğer
            sorted_links = sorted(links, key=lambda x: (
                2 if "sibnet" in str(x.get("data-frame")) else
                1 if "cizgi" in str(x.get("data-frame")) else 0
            ), reverse=True)

            found_url = None
            for link in sorted_links:
                src = link.get("data-frame")
                found_url = await resolve_video(session, src, page_url)
                if found_url: break
            
            if found_url:
                FINAL_PLAYLIST.append({
                    "group": category,
                    "title": full_title,
                    "logo": poster,
                    "url": found_url
                })
                
        PROCESSED += 1
        if PROCESSED % 50 == 0:
            print(f"İlerleme: {PROCESSED}/{TOTAL_URLS} tamamlandı...", flush=True)
        
        URL_QUEUE.task_done()

# --- SITEMAP TARAYICI ---
async def load_sitemaps(session):
    global TOTAL_URLS
    print("Sitemap İndiriliyor...", flush=True)
    
    # Ana Index
    index_xml = await fetch_text(session, SITEMAP_INDEX)
    if not index_xml:
        print("!!! Sitemap ana dizini çekilemedi. IP Ban olabilir.", flush=True)
        return False

    # XML Namespace temizliği
    index_xml = re.sub(r' xmlns="[^"]+"', '', index_xml, count=1)
    try:
        root = ET.fromstring(index_xml)
    except:
        print("!!! Sitemap XML formatı bozuk.", flush=True)
        return False

    target_sitemaps = []
    
    # Alt sitemapleri bul (post-sitemap veya diziler-sitemap)
    for sitemap in root.findall("sitemap"):
        loc = sitemap.find("loc").text
        if "post-sitemap" in loc or "diziler-sitemap" in loc:
            target_sitemaps.append(loc)
    
    # Limit koy (En güncel X sitemap)
    # Site haritası genelde eskiden yeniye sıralıdır, o yüzden listeyi ters çevirip ilk X taneyi alalım.
    # CizgiMax'te post-sitemap.xml (numarasız) en güncelidir, sonra 2, 3...
    # O yüzden sıralama zaten genelde doğrudur ama kontrol etmekte fayda var.
    
    # Sitemaps'i al ve işle
    # Sadece ilk 'SITEMAP_LIMIT' kadarını al (Zaman kısıtlaması için)
    selected_sitemaps = target_sitemaps[:SITEMAP_LIMIT]
    print(f"Hedeflenen Sitemap Sayısı: {len(selected_sitemaps)} (Toplam: {len(target_sitemaps)})", flush=True)

    for sub_url in selected_sitemaps:
        print(f" > Linkler toplanıyor: {sub_url}", flush=True)
        sub_xml = await fetch_text(session, sub_url)
        if not sub_xml: continue
        
        try:
            sub_xml = re.sub(r' xmlns="[^"]+"', '', sub_xml, count=1)
            sub_root = ET.fromstring(sub_xml)
            
            count = 0
            for url_tag in sub_root.findall("url"):
                loc = url_tag.find("loc").text
                # Sadece izleme sayfaları
                if "-izle" in loc or "bolum" in loc:
                    URL_QUEUE.put_nowait(loc)
                    count += 1
            TOTAL_URLS += count
        except: pass
        
    print(f"Toplam {TOTAL_URLS} adet bölüm linki kuyruğa eklendi.", flush=True)
    return True

async def main():
    print("CizgiMax Hibrit Bot (Sitemap + AES) Başlatılıyor...", flush=True)
    start_time = time.time()
    
    # Tek Oturum (Cookie Korumalı)
    async with AsyncSession(impersonate="chrome120") as session:
        
        # 1. Aşama: Linkleri Topla
        success = await load_sitemaps(session)
        
        if not success or TOTAL_URLS == 0:
            print("Sitemap başarısız, Kategori Moduna geçiliyor...", flush=True)
            # Burada yedek kategori tarama kodu çalışabilir ama şimdilik sitemap'e odaklanalım.
            return

        # 2. Aşama: İşçileri Başlat (Worker Pool)
        print(f"{CONCURRENT_LIMIT} İşçi başlatılıyor...", flush=True)
        workers = []
        for i in range(CONCURRENT_LIMIT):
            workers.append(asyncio.create_task(worker(i, session)))
        
        # Kuyruğun bitmesini bekle
        await URL_QUEUE.join()
        
        # İşçileri durdur
        for w in workers: w.cancel()
    
    # 3. Aşama: Kaydet
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik bulundu. M3U yazılıyor...", flush=True)
    
    # HTML Player için Gruplama/Sıralama
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

    print(f"İşlem Tamamlandı! Süre: {time.time() - start_time:.2f}sn")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt: pass
