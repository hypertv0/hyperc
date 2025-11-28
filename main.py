import asyncio
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
import re
import time
import base64
import sys
import random
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# --- AYARLAR ---
BASE_URL = "https://cizgimax.online"
OUTPUT_FILE = "cizgimax.m3u"
WORKER_COUNT = 20       # Aynı anda çalışacak işçi sayısı (Sabit Hız)
MAX_PAGES = 3           # Her kategoriden taranacak sayfa sayısı
NETWORK_TIMEOUT = 15    # İstek zaman aşımı

CATEGORIES = {
    "Son Eklenenler": f"{BASE_URL}/diziler/?orderby=date&order=DESC",
    "Aile": f"{BASE_URL}/diziler/?s_type&tur[0]=aile&orderby=date&order=DESC",
    "Aksiyon": f"{BASE_URL}/diziler/?s_type&tur[0]=aksiyon-macera&orderby=date&order=DESC",
    "Animasyon": f"{BASE_URL}/diziler/?s_type&tur[0]=animasyon&orderby=date&order=DESC",
    "Bilim Kurgu": f"{BASE_URL}/diziler/?s_type&tur[0]=bilim-kurgu-fantazi&orderby=date&order=DESC",
    "Çocuklar": f"{BASE_URL}/diziler/?s_type&tur[0]=cocuklar&orderby=date&order=DESC",
    "Komedi": f"{BASE_URL}/diziler/?s_type&tur[0]=komedi&orderby=date&order=DESC"
}

# Sonuçları burada toplayacağız
FINAL_PLAYLIST = []
# Kuyruk (İşlenecek Bölümler Burada Bekler)
QUEUE = asyncio.Queue()
# Toplam bulunan bölüm sayısı (İlerleme çubuğu için)
TOTAL_FOUND = 0
PROCESSED_COUNT = 0

# --- ŞİFRE ÇÖZME ---
def decrypt_aes_cizgiduo(encrypted_data, password):
    try:
        key_bytes = password.encode('utf-8')
        if len(key_bytes) > 32: key_bytes = key_bytes[:32]
        elif len(key_bytes) < 32: key_bytes = key_bytes.ljust(32, b'\0')
        
        iv_bytes = key_bytes[:16]
        encrypted_bytes = base64.b64decode(encrypted_data)
        cipher = AES.new(key_bytes, AES.MODE_CBC, iv=iv_bytes)
        try:
            decrypted_text = unpad(cipher.decrypt(encrypted_bytes), AES.block_size).decode('utf-8')
        except:
            decrypted_text = cipher.decrypt(encrypted_bytes).decode('utf-8', errors='ignore').strip().replace('\x00', '')
        return decrypted_text
    except:
        return None

# --- AĞ İSTEKLERİ (Safe Fetch) ---
async def fetch_text(session, url, referer=None):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer if referer else BASE_URL
    }
    try:
        # Timeout'u Python seviyesinde zorla (Donmayı engeller)
        async with asyncio.timeout(NETWORK_TIMEOUT):
            response = await session.get(url, headers=headers, impersonate="chrome110")
            if response.status_code == 200:
                return response.text
    except:
        pass
    return None

# --- ÇÖZÜCÜLER ---
async def resolve_video_url(session, iframe_src, referer_url):
    if not iframe_src: return None
    if iframe_src.startswith("//"): iframe_src = "https:" + iframe_src

    # 1. CizgiDuo/Pass
    if "cizgiduo" in iframe_src or "cizgipass" in iframe_src:
        html = await fetch_text(session, iframe_src, referer=referer_url)
        if html:
            match = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
            if match:
                pwd = match.group(1)
                data_match = re.search(r'"data"\s*:\s*"([^"]+)"', match.group(2))
                if data_match:
                    dec = decrypt_aes_cizgiduo(data_match.group(1), pwd)
                    if dec:
                        m3u = re.search(r'video_location":"([^"]+)"', dec)
                        if m3u: return m3u.group(1).replace("\\", "")

    # 2. Sibnet
    elif "sibnet.ru" in iframe_src:
        if "|" in iframe_src: iframe_src = iframe_src.split("|")[0]
        vid_id = None
        if "videoid=" in iframe_src: vid_id = re.search(r'videoid=(\d+)', iframe_src)
        elif "/video/" in iframe_src: vid_id = re.search(r'/video/(\d+)', iframe_src)
        
        if vid_id:
            real_url = f"https://video.sibnet.ru/video/{vid_id.group(1)}"
            html = await fetch_text(session, real_url, referer=referer_url)
            if html:
                slug = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html)
                if slug:
                    s = slug.group(1)
                    return f"https://video.sibnet.ru{s}" if s.startswith("/") else s

    # 3. Genel
    else:
        html = await fetch_text(session, iframe_src, referer=referer_url)
        if html:
            m3u = re.search(r'(https?://[^"\'\s]+\.m3u8)', html)
            if m3u: return m3u.group(1)
            mp4 = re.search(r'(https?://[^"\'\s]+\.mp4)', html)
            if mp4: return mp4.group(1)
            
    return None

# --- WORKER (İŞÇİ) FONKSİYONU ---
async def worker(worker_id, session):
    global PROCESSED_COUNT
    while True:
        # Kuyruktan bir görev al
        item = await QUEUE.get()
        
        # item: (category, series_title, ep_title, ep_url, poster)
        category, series_title, ep_title, ep_url, poster = item
        
        try:
            html = await fetch_text(session, ep_url, referer=BASE_URL)
            found_url = None
            
            if html:
                soup = BeautifulSoup(html, 'html.parser')
                links = soup.select("ul.linkler li a")
                
                # Linkleri sırala
                sorted_links = sorted(links, key=lambda x: (
                    2 if "cizgi" in str(x.get("data-frame")) else
                    1 if "sibnet" in str(x.get("data-frame")) else 0
                ), reverse=True)

                for link in sorted_links:
                    src = link.get("data-frame")
                    video_url = await resolve_video_url(session, src, ep_url)
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
                
            PROCESSED_COUNT += 1
            if PROCESSED_COUNT % 10 == 0:
                print(f"İlerleme: {PROCESSED_COUNT}/{TOTAL_FOUND} tamamlandı. (Kuyruk: {QUEUE.qsize()})", flush=True)

        except Exception as e:
            # print(f"Hata: {e}")
            pass
        finally:
            # Görevin bittiğini bildir
            QUEUE.task_done()

# --- TARAYICILAR (PRODUCERS) ---
async def scan_series(session, category, series_title, series_url, poster):
    global TOTAL_FOUND
    html = await fetch_text(session, series_url)
    if not html: return

    soup = BeautifulSoup(html, 'html.parser')
    ep_divs = soup.select("div.asisotope div.ajax_post")
    
    for div in ep_divs:
        name_s = div.select_one("span.episode-names")
        link_a = div.select_one("a")
        if name_s and link_a:
            # Kuyruğa at (İşçiler buradan alacak)
            QUEUE.put_nowait((category, series_title, name_s.text.strip(), link_a['href'], poster))
            TOTAL_FOUND += 1

async def scan_category(session, cat_name, cat_url):
    print(f"--- Kategori Taranıyor: {cat_name} ---", flush=True)
    for page in range(1, MAX_PAGES + 1):
        if page == 1: url = cat_url
        else:
            if "/?p=" in cat_url: parts = cat_url.split("/?p="); url = f"{parts[0]}/page/{page}/?p={parts[1]}"
            else: parts = cat_url.split("/?"); base = parts[0]; query = "/?" + parts[1] if len(parts) > 1 else ""; base = base[:-1] if base.endswith("/") else base; url = f"{base}/page/{page}{query}"
        
        html = await fetch_text(session, url)
        if not html: break
        
        soup = BeautifulSoup(html, 'html.parser')
        items = soup.select("ul.filter-results li")
        if not items: break
        
        for item in items:
            t_tag = item.select_one("h2.truncate")
            l_tag = item.select_one("div.poster-subject a")
            i_tag = item.select_one("div.poster-media img")
            if t_tag and l_tag:
                poster = i_tag.get("data-src") if i_tag else ""
                await scan_series(session, cat_name, t_tag.text.strip(), l_tag['href'], poster)

async def monitor():
    """Kullanıcıya bilgi vermek için döngü"""
    while not QUEUE.empty():
        await asyncio.sleep(5)
        # print(f"Aktif... İşlenen: {PROCESSED_COUNT}/{TOTAL_FOUND}", flush=True)

async def main():
    print("Stabil Kuyruk Sistemi Başlatılıyor...", flush=True)
    start_time = time.time()
    
    async with AsyncSession() as session:
        # 1. Aşama: Tüm Linkleri Bul (Discovery Phase)
        print("Linkler Toplanıyor...", flush=True)
        scan_tasks = []
        for c_name, c_url in CATEGORIES.items():
            scan_tasks.append(scan_category(session, c_name, c_url))
        await asyncio.gather(*scan_tasks)
        
        print(f"\nToplam {TOTAL_FOUND} bölüm bulundu. İşleniyor...", flush=True)
        
        # 2. Aşama: İşçileri Başlat (Worker Phase)
        workers = []
        for i in range(WORKER_COUNT):
            workers.append(asyncio.create_task(worker(i, session)))
        
        # Kuyruğun bitmesini bekle
        await QUEUE.join()
        
        # İşçileri durdur
        for w in workers: w.cancel()
    
    # 3. Aşama: Kaydet
    print(f"\nİşlem Tamamlandı. {len(FINAL_PLAYLIST)}/{TOTAL_FOUND} video başarıyla alındı.", flush=True)
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

    print(f"Süre: {time.time() - start_time:.2f}sn")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt: pass
