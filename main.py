import asyncio
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
import re
import time
import base64
import sys
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# --- AYARLAR ---
BASE_URL = "https://cizgimax.online"
OUTPUT_FILE = "cizgimax.m3u"
CONCURRENT_LIMIT = 40   # ÇOK HIZLI: Aynı anda 40 bağlantı
MAX_PAGES = 3           # Derinlik
TASK_TIMEOUT = 20       # 20 saniyede bitmeyen işlemi öldür (Donmayı engeller)

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

# --- ŞİFRE ÇÖZÜCÜ ---
def decrypt_aes(encrypted, password):
    try:
        key = password.encode('utf-8')
        if len(key) > 32: key = key[:32]
        elif len(key) < 32: key = key.ljust(32, b'\0')
        iv = key[:16]
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        try:
            dec = unpad(cipher.decrypt(base64.b64decode(encrypted)), AES.block_size)
        except:
            dec = cipher.decrypt(base64.b64decode(encrypted))
        return dec.decode('utf-8', 'ignore').strip().replace('\x00', '')
    except:
        return None

# --- AĞ İSTEKLERİ ---
async def fetch_text(session, url, referer=None):
    headers = {"Referer": referer} if referer else {}
    try:
        response = await session.get(url, headers=headers, impersonate="chrome110", timeout=10)
        if response.status_code == 200:
            return response.text
    except:
        pass
    return None

# --- VİDEO ÇÖZÜCÜ ---
async def get_video_link(session, iframe_src, referer_url):
    if not iframe_src: return None
    if iframe_src.startswith("//"): iframe_src = "https:" + iframe_src

    # 1. CizgiDuo
    if "cizgi" in iframe_src:
        html = await fetch_text(session, iframe_src, referer=referer_url)
        if html:
            m = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
            if m:
                dm = re.search(r'"data"\s*:\s*"([^"]+)"', m.group(2))
                if dm:
                    dec = decrypt_aes(dm.group(1), m.group(1))
                    if dec:
                        res = re.search(r'video_location":"([^"]+)"', dec)
                        if res: return res.group(1).replace("\\", "")

    # 2. Sibnet
    elif "sibnet" in iframe_src:
        if "|" in iframe_src: iframe_src = iframe_src.split("|")[0]
        vid = None
        if "videoid=" in iframe_src: vid = re.search(r'videoid=(\d+)', iframe_src)
        elif "/video/" in iframe_src: vid = re.search(r'/video/(\d+)', iframe_src)
        
        if vid:
            html = await fetch_text(session, f"https://video.sibnet.ru/video/{vid.group(1)}", referer=referer_url)
            if html:
                slug = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html)
                if slug:
                    return f"https://video.sibnet.ru{slug.group(1)}" if slug.group(1).startswith("/") else slug.group(1)

    # 3. Genel
    else:
        html = await fetch_text(session, iframe_src, referer=referer_url)
        if html:
            m3u = re.search(r'(https?://[^"\'\s]+\.m3u8)', html)
            if m3u: return m3u.group(1)
            mp4 = re.search(r'(https?://[^"\'\s]+\.mp4)', html)
            if mp4: return mp4.group(1)
            
    return None

# --- ANA İŞLEM ---
async def process_episode_safe(session, semaphore, category, series_title, ep_title, ep_url, poster):
    # Bu fonksiyon Semafor ile korunur (Aynı anda max 40 tane çalışır)
    async with semaphore:
        try:
            # wait_for: İşlem 20 saniyede bitmezse iptal et (Anti-Freeze)
            await asyncio.wait_for(
                _process_episode_logic(session, category, series_title, ep_title, ep_url, poster),
                timeout=TASK_TIMEOUT
            )
        except asyncio.TimeoutError:
            # print(f"  [!] Zaman Aşımı: {ep_title}", flush=True)
            pass
        except Exception:
            pass

async def _process_episode_logic(session, category, series_title, ep_title, ep_url, poster):
    html = await fetch_text(session, ep_url, referer=BASE_URL)
    if not html: return

    soup = BeautifulSoup(html, 'html.parser')
    links = soup.select("ul.linkler li a")
    
    # Sibnet ve CizgiDuo'yu öne al
    sorted_links = sorted(links, key=lambda x: (
        2 if "cizgi" in str(x.get("data-frame")) else
        1 if "sibnet" in str(x.get("data-frame")) else 0
    ), reverse=True)

    found = None
    for link in sorted_links:
        src = link.get("data-frame")
        found = await get_video_link(session, src, ep_url)
        if found: break
    
    if found:
        clean_ep = ep_title.replace(series_title, "").strip()
        if not clean_ep: clean_ep = ep_title
        full_title = f"{series_title} - {clean_ep}"
        
        FINAL_PLAYLIST.append({
            "group": category,
            "title": full_title,
            "logo": poster,
            "url": found
        })
        print(f"  [+] {full_title}", flush=True)

async def scan_series(session, semaphore, category, series_title, series_url, poster):
    html = await fetch_text(session, series_url)
    if not html: return

    soup = BeautifulSoup(html, 'html.parser')
    ep_divs = soup.select("div.asisotope div.ajax_post")
    
    if ep_divs:
        print(f" > {series_title} ({len(ep_divs)} Bölüm)", flush=True)
        
    tasks = []
    for div in ep_divs:
        name = div.select_one("span.episode-names")
        link = div.select_one("a")
        if name and link:
            tasks.append(process_episode_safe(
                session, semaphore, category, series_title, 
                name.text.strip(), link['href'], poster
            ))
    
    if tasks:
        await asyncio.gather(*tasks)

async def scan_category(session, semaphore, cat_name, cat_url):
    print(f"--- {cat_name} ---", flush=True)
    series_tasks = []
    
    for page in range(1, MAX_PAGES + 1):
        if page == 1: url = cat_url
        else:
            if "/?p=" in cat_url: parts = cat_url.split("/?p="); url = f"{parts[0]}/page/{page}/?p={parts[1]}"
            else: parts = cat_url.split("/?"); url = f"{parts[0]}/page/{page}{'/?'+parts[1] if len(parts)>1 else ''}"
        
        print(f" >> Sayfa {page}...", flush=True)
        html = await fetch_text(session, url)
        if not html: break
        
        soup = BeautifulSoup(html, 'html.parser')
        items = soup.select("ul.filter-results li")
        if not items: break
        
        for item in items:
            t = item.select_one("h2.truncate")
            l = item.select_one("div.poster-subject a")
            i = item.select_one("div.poster-media img")
            if t and l:
                poster = i.get("data-src") if i else ""
                series_tasks.append(scan_series(
                    session, semaphore, cat_name, 
                    t.text.strip(), l['href'], poster
                ))
    
    await asyncio.gather(*series_tasks)

async def main():
    print("Turbo CizgiMax (Timeout Korumalı) Başlatılıyor...", flush=True)
    start = time.time()
    
    # 40 İşlem Limiti
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    async with AsyncSession() as session:
        tasks = []
        for c, u in CATEGORIES.items():
            tasks.append(scan_category(session, semaphore, c, u))
        await asyncio.gather(*tasks)
    
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik. Kaydediliyor...", flush=True)
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

    print(f"Süre: {time.time() - start:.2f}sn")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt: pass
