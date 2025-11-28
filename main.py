import asyncio
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
import re
import time
import base64
import sys
import random
import json
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# --- AYARLAR ---
BASE_URL = "https://cizgimax.online"
OUTPUT_FILE = "cizgimax.m3u"
CONCURRENT_LIMIT = 15   # Cloudflare'i tetiklememek için makul hız
MAX_PAGES = 3           # Tarama derinliği
TIMEOUT = 30            # Zaman aşımı

# Kotlin kodundaki kategoriler
CATEGORIES = {
    "Son Eklenenler": f"{BASE_URL}/diziler/?orderby=date&order=DESC",
    "Aile": f"{BASE_URL}/diziler/?s_type&tur[0]=aile&orderby=date&order=DESC",
    "Aksiyon": f"{BASE_URL}/diziler/?s_type&tur[0]=aksiyon-macera&orderby=date&order=DESC",
    "Animasyon": f"{BASE_URL}/diziler/?s_type&tur[0]=animasyon&orderby=date&order=DESC",
    "Bilim Kurgu": f"{BASE_URL}/diziler/?s_type&tur[0]=bilim-kurgu-fantazi&orderby=date&order=DESC",
    "Çocuklar": f"{BASE_URL}/diziler/?s_type&tur[0]=cocuklar&orderby=date&order=DESC",
    "Komedi": f"{BASE_URL}/diziler/?s_type&tur[0]=komedi&orderby=date&order=DESC"
}

FINAL_PLAYLIST = []

# --- KOTLIN AES PORTU (CizgiDuo) ---
def decrypt_aes(encrypted, password):
    """
    Kotlin: AesHelper.cryptoAESHandler(data, pass, false)
    Logic: AES/CBC/PKCS5Padding, Key=Password(32byte), IV=Key(16byte)
    """
    try:
        key = password.encode('utf-8')
        # Key padding to 32 bytes
        if len(key) > 32: key = key[:32]
        elif len(key) < 32: key = key.ljust(32, b'\0')
        
        iv = key[:16]
        
        encrypted_bytes = base64.b64decode(encrypted)
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        
        try:
            decrypted = unpad(cipher.decrypt(encrypted_bytes), AES.block_size)
        except ValueError:
            # Padding hatası olursa ham veriyi al ve temizle
            decrypted = cipher.decrypt(encrypted_bytes)
            
        return decrypted.decode('utf-8', errors='ignore').strip().replace('\x00', '')
    except Exception as e:
        return None

# --- AĞ İSTEKLERİ (Cloudflare Bypass) ---
async def fetch_text(session, url, referer=None):
    # Gerçek bir Android/Chrome gibi görünelim
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": referer if referer else BASE_URL,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Upgrade-Insecure-Requests": "1"
    }
    
    try:
        # Impersonate 'chrome120' Cloudflare'i geçmek için en iyi yöntemlerden biri
        response = await session.get(url, headers=headers, timeout=TIMEOUT, impersonate="chrome120")
        
        # DEBUG İÇİN ÖNEMLİ:
        if response.status_code == 403:
            # print(f"BLOKLANDI (403): {url}")
            return None
        
        # Cloudflare "Just a moment" sayfası mı geldi?
        if "<title>Just a moment...</title>" in response.text:
            print(f"CLOUDFLARE YAKALADI: {url}")
            return None
            
        if response.status_code == 200:
            return response.text
            
    except Exception as e:
        pass
        
    return None

# --- KAYNAK ÇÖZÜCÜLER ---
async def resolve_video_source(session, iframe_src, referer_url):
    """
    Kotlin kodundaki mantığa göre kaynakları çözer.
    """
    if not iframe_src: return None
    if iframe_src.startswith("//"): iframe_src = "https:" + iframe_src

    # 1. CizgiDuo / CizgiPass (Kotlin: CizgiDuo.kt)
    if "cizgiduo" in iframe_src or "cizgipass" in iframe_src:
        html = await fetch_text(session, iframe_src, referer=referer_url)
        if html:
            # Regex: bePlayer('pass', '{data}')
            match = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
            if match:
                password = match.group(1)
                json_raw = match.group(2)
                # JSON içindeki "data" alanını bul
                data_match = re.search(r'"data"\s*:\s*"([^"]+)"', json_raw)
                if data_match:
                    decrypted = decrypt_aes(data_match.group(1), password)
                    if decrypted:
                        # video_location":"url"
                        m3u = re.search(r'video_location":"([^"]+)"', decrypted)
                        if m3u: return m3u.group(1).replace("\\", "")

    # 2. Sibnet (Kotlin: SibNet.kt)
    elif "sibnet.ru" in iframe_src:
        # Link temizliği
        clean_url = iframe_src.split("|")[0]
        
        # ID çıkarma
        video_id = None
        if "videoid=" in clean_url:
            video_id = re.search(r'videoid=(\d+)', clean_url).group(1)
        elif "/video/" in clean_url:
            video_id = re.search(r'/video/(\d+)', clean_url).group(1)
            
        if video_id:
            # Sibnet shell yerine video sayfasına gidiyoruz (Bot korumasını aşmak için)
            real_url = f"https://video.sibnet.ru/video/{video_id}"
            html = await fetch_text(session, real_url, referer=referer_url)
            if html:
                slug = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html)
                if slug:
                    path = slug.group(1)
                    return f"https://video.sibnet.ru{path}" if path.startswith("/") else path

    # 3. Genel Yedek (M3U8/MP4 bulursa alır)
    else:
        html = await fetch_text(session, iframe_src, referer=referer_url)
        if html:
            m3u = re.search(r'(https?://[^"\'\s]+\.m3u8)', html)
            if m3u: return m3u.group(1)
            mp4 = re.search(r'(https?://[^"\'\s]+\.mp4)', html)
            if mp4: return mp4.group(1)

    return None

# --- BÖLÜM İŞLEME ---
async def process_episode(session, semaphore, category, series_title, ep_title, ep_url, poster):
    async with semaphore: # Eşzamanlılık limiti
        html = await fetch_text(session, ep_url, referer=BASE_URL)
        if not html: return

        soup = BeautifulSoup(html, 'html.parser')
        # Kotlin: document.select("ul.linkler li")
        links = soup.select("ul.linkler li a")
        
        # Linkleri sırala: Sibnet > Cizgi > Diğer
        sorted_links = sorted(links, key=lambda x: (
            2 if "sibnet" in str(x.get("data-frame")) else
            1 if "cizgi" in str(x.get("data-frame")) else 0
        ), reverse=True)

        found_url = None
        for link in sorted_links:
            src = link.get("data-frame")
            found_url = await resolve_video_source(session, src, ep_url)
            if found_url: break
        
        if found_url:
            # İsim Temizliği: "Gumball - 1. Bölüm" formatı
            clean_ep = ep_title.replace(series_title, "").strip()
            # Başta "-" veya ":" varsa temizle
            clean_ep = re.sub(r'^[-:\s]+', '', clean_ep)
            if not clean_ep: clean_ep = ep_title
            
            full_title = f"{series_title} - {clean_ep}"
            
            FINAL_PLAYLIST.append({
                "group": category,
                "title": full_title,
                "logo": poster,
                "url": found_url
            })
            print(f"  [+] {full_title}", flush=True)

# --- DİZİ İŞLEME ---
async def process_series(session, semaphore, category, series_title, series_url, poster):
    html = await fetch_text(session, series_url)
    if not html: return

    soup = BeautifulSoup(html, 'html.parser')
    # Kotlin: document.select("div.asisotope div.ajax_post")
    ep_divs = soup.select("div.asisotope div.ajax_post")
    
    if ep_divs:
        print(f" > {series_title} ({len(ep_divs)} Bölüm)", flush=True)
        
    tasks = []
    for div in ep_divs:
        name_s = div.select_one("span.episode-names")
        link_a = div.select_one("a")
        
        if name_s and link_a:
            tasks.append(process_episode(
                session, semaphore, category, series_title, 
                name_s.text.strip(), link_a['href'], poster
            ))
            
    # Dizi içindeki bölümleri paralel işle
    if tasks:
        await asyncio.gather(*tasks)

# --- KATEGORİ TARAMA ---
async def scan_category(session, semaphore, cat_name, cat_url):
    print(f"--- {cat_name} Taranıyor ---", flush=True)
    
    series_tasks = []
    
    for page in range(1, MAX_PAGES + 1):
        # Sayfalama URL mantığı
        if page == 1: url = cat_url
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
        
        print(f" >> Sayfa {page}...", flush=True)
        html = await fetch_text(session, url)
        
        if not html:
            # DEBUG: Sayfa boş geldiyse neden?
            print(f"!!! Sayfa {page} açılamadı (Cloudflare/403/Boş).")
            break
        
        soup = BeautifulSoup(html, 'html.parser')
        # Kotlin: document.select("ul.filter-results li")
        items = soup.select("ul.filter-results li")
        
        if not items:
            print("   -> Bu sayfada içerik bulunamadı (Selector eşleşmedi).")
            break
        
        for item in items:
            t = item.select_one("h2.truncate")
            l = item.select_one("div.poster-subject a")
            i = item.select_one("div.poster-media img")
            
            if t and l:
                poster = i.get("data-src") if i else ""
                series_tasks.append(process_series(
                    session, semaphore, cat_name, 
                    t.text.strip(), l['href'], poster
                ))
    
    # Tüm dizileri bekle
    if series_tasks:
        await asyncio.gather(*series_tasks)

async def main():
    print("CizgiMax Final Bot v5 Başlatılıyor...", flush=True)
    start_time = time.time()
    
    # Eşzamanlılık Limiti (15 güvenlidir)
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    # Tek bir Session oluşturuyoruz (Cookies ve Headers korunur)
    async with AsyncSession(impersonate="chrome120") as session:
        cat_tasks = []
        for c, u in CATEGORIES.items():
            cat_tasks.append(scan_category(session, semaphore, c, u))
        
        await asyncio.gather(*cat_tasks)
    
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik bulundu. M3U yazılıyor...", flush=True)
    
    # Gruplama ve Sıralama
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
    try:
        asyncio.run(main())
    except KeyboardInterrupt: pass
