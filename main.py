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
CONCURRENT_LIMIT = 15   # 15 İdealdir (Siteyi kızdırmaz)
MAX_PAGES = 3           # Tarama Sayfası
TIMEOUT = 25            # Bekleme süresini uzattık (Yavaş site için)

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

# --- AĞ İSTEĞİ (OTURUM KORUMALI) ---
async def fetch_text(session, url, referer=None):
    headers = {
        "Referer": referer if referer else BASE_URL,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        # Session cookie'lerini korur, her seferinde yeni browser açmaz
        response = await session.get(url, headers=headers, timeout=TIMEOUT)
        if response.status_code == 200:
            return response.text
        elif response.status_code == 403:
            # print(f"  [!] 403 Erişim Engeli: {url}")
            pass
    except Exception:
        pass
    return None

# --- VİDEO ÇÖZÜCÜ ---
async def resolve_video(session, iframe_url, referer_url):
    if not iframe_url: return None
    if iframe_url.startswith("//"): iframe_url = "https:" + iframe_url

    # 1. CizgiDuo / CizgiPass
    if "cizgi" in iframe_url:
        html = await fetch_text(session, iframe_url, referer=referer_url)
        if html:
            # Şifreli Veri
            m = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
            if m:
                dm = re.search(r'"data"\s*:\s*"([^"]+)"', m.group(2))
                if dm:
                    dec = decrypt_aes(dm.group(1), m.group(1))
                    if dec:
                        res = re.search(r'video_location":"([^"]+)"', dec)
                        if res: return res.group(1).replace("\\", "")
            
            # Şifresiz Açık Veri (Bazen şifresiz olur)
            direct_m3u = re.search(r'(https?://[^"\'\s]+\.m3u8)', html)
            if direct_m3u: return direct_m3u.group(1)

    # 2. Sibnet
    elif "sibnet" in iframe_url:
        if "|" in iframe_url: iframe_url = iframe_url.split("|")[0]
        vid = None
        if "videoid=" in iframe_url: vid = re.search(r'videoid=(\d+)', iframe_url)
        elif "/video/" in iframe_url: vid = re.search(r'/video/(\d+)', iframe_url)
        
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

# --- İŞLEMCİLER ---
async def process_episode(session, semaphore, category, series_title, ep_title, ep_url, poster):
    async with semaphore:
        html = await fetch_text(session, ep_url, referer=BASE_URL)
        if not html: return

        soup = BeautifulSoup(html, 'html.parser')
        links = soup.select("ul.linkler li a")
        
        # Öncelik Sıralaması: Sibnet > Cizgi > Diğer
        # Sibnet GitHub'a daha az engel koyar, onu öne alıyoruz.
        sorted_links = sorted(links, key=lambda x: (
            2 if "sibnet" in str(x.get("data-frame")) else
            1 if "cizgi" in str(x.get("data-frame")) else 0
        ), reverse=True)

        found = None
        for link in sorted_links:
            src = link.get("data-frame")
            found = await resolve_video(session, src, ep_url)
            if found: break
        
        if found:
            clean_ep = ep_title.replace(series_title, "").strip()
            if not clean_ep: clean_ep = ep_title
            
            # Bölüm ismi sayı ise başına 'Bölüm' ekle (Estetik)
            if clean_ep.isdigit(): clean_ep = f"Bölüm {clean_ep}"
            
            full_title = f"{series_title} - {clean_ep}"
            
            FINAL_PLAYLIST.append({
                "group": category,
                "title": full_title,
                "logo": poster,
                "url": found
            })
            print(f"  [+] {full_title}", flush=True)
        # else: print(f"  [-] {ep_title} (Bulunamadı)", flush=True)

async def process_series(session, semaphore, category, series_title, series_url, poster):
    # Dizi sayfasına gir (Semafor kullanma, sadece fetch'de var)
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
            tasks.append(process_episode(
                session, semaphore, category, series_title, 
                name.text.strip(), link['href'], poster
            ))
            
    if tasks:
        await asyncio.gather(*tasks)

async def scan_category(session, semaphore, cat_name, cat_url):
    print(f"--- {cat_name} Taranıyor ---", flush=True)
    series_tasks = []
    
    for page in range(1, MAX_PAGES + 1):
        if page == 1: url = cat_url
        else:
            if "/?p=" in cat_url: parts = cat_url.split("/?p="); url = f"{parts[0]}/page/{page}/?p={parts[1]}"
            else: parts = cat_url.split("/?"); url = f"{parts[0]}/page/{page}{'/?'+parts[1] if len(parts)>1 else ''}"
        
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
                series_tasks.append(process_series(
                    session, semaphore, cat_name, 
                    t.text.strip(), l['href'], poster
                ))
    
    if series_tasks:
        await asyncio.gather(*series_tasks)

async def main():
    print("Stabil CizgiMax Bot Başlatılıyor...", flush=True)
    start = time.time()
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    # TEK OTURUM (impersonate='chrome124')
    # Bu oturum tüm isteklerde korunur, böylece Cloudflare çerezleri saklanır.
    async with AsyncSession(impersonate="chrome124") as session:
        cat_tasks = []
        for c, u in CATEGORIES.items():
            cat_tasks.append(scan_category(session, semaphore, c, u))
        await asyncio.gather(*cat_tasks)
    
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik. M3U yazılıyor...", flush=True)
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
