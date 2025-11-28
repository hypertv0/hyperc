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
CONCURRENT_LIMIT = 50   # ÇOK HIZLI (50 Bağlantı)
MAX_PAGES = 3           # Tarama Derinliği
TIMEOUT = 8             # 8 saniyede açılmayan linki atla (Hız için)

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

# --- YARDIMCI FONKSİYONLAR ---
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

async def fetch_text(session, url, referer=None):
    headers = {"Referer": referer} if referer else {}
    try:
        # Rastgele tarayıcı imzası ile engelleri aşmayı dene
        impersonate_list = ["chrome110", "chrome120", "edge101", "safari15_3"]
        response = await session.get(url, headers=headers, timeout=TIMEOUT, impersonate=random.choice(impersonate_list))
        if response.status_code == 200: return response.text
    except: pass
    return None

# --- VİDEO ÇÖZÜCÜ (GENEL TARAMA) ---
async def extract_video(session, iframe_url, referer_url):
    if not iframe_url: return None
    if iframe_url.startswith("//"): iframe_url = "https:" + iframe_url

    # Sayfa kaynağını çek
    html = await fetch_text(session, iframe_url, referer=referer_url)
    if not html: return None

    # 1. YÖNTEM: CizgiDuo Şifre Çözme
    if "cizgi" in iframe_url:
        match = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
        if match:
            dm = re.search(r'"data"\s*:\s*"([^"]+)"', match.group(2))
            if dm:
                dec = decrypt_aes(dm.group(1), match.group(1))
                if dec:
                    res = re.search(r'video_location":"([^"]+)"', dec)
                    if res: return res.group(1).replace("\\", "")

    # 2. YÖNTEM: Sibnet (Link Düzeltme)
    if "sibnet" in iframe_url:
        slug = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html)
        if slug:
            s = slug.group(1)
            return f"https://video.sibnet.ru{s}" if s.startswith("/") else s

    # 3. YÖNTEM: KÖR TARAMA (Universal Regex)
    # Sayfadaki HERHANGİ bir .m3u8 linkini bul
    m3u8_matches = re.findall(r'(https?://[^"\'\s]+\.m3u8)', html)
    for m in m3u8_matches:
        return m # İlk bulduğunu döndür

    # Sayfadaki HERHANGİ bir .mp4 linkini bul
    mp4_matches = re.findall(r'(https?://[^"\'\s]+\.mp4)', html)
    for m in mp4_matches:
        return m

    return None

# --- BÖLÜM İŞLEYİCİ ---
async def process_episode(session, semaphore, category, series_title, ep_title, ep_url, poster):
    async with semaphore: # Havuz limiti
        html = await fetch_text(session, ep_url, referer=BASE_URL)
        if not html: return

        soup = BeautifulSoup(html, 'html.parser')
        links = soup.select("ul.linkler li a")
        
        # Linkleri topla
        iframe_list = []
        for l in links:
            src = l.get("data-frame")
            if src: iframe_list.append(src)
        
        # Sibnet'i ve Cizgi'yi öne al (Daha hızlı açılırlar)
        iframe_list.sort(key=lambda x: 0 if "sibnet" in x or "cizgi" in x else 1)

        found_url = None
        for src in iframe_list:
            found_url = await extract_video(session, src, ep_url)
            if found_url: break # Bulduysan hemen çık
        
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
            print(f"  [+] {full_title}", flush=True)
        # else: print(f"  [-] Yok: {ep_title}", flush=True)

async def process_series(session, semaphore, category, series_title, series_url, poster):
    # Dizi sayfasını çek
    html = await fetch_text(session, series_url)
    if not html: return

    soup = BeautifulSoup(html, 'html.parser')
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
            
    # Hepsini aynı anda havuza at (Semaphore ile sınırlanacak)
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
    
    # Tüm dizileri paralel işle
    if series_tasks:
        await asyncio.gather(*series_tasks)

async def main():
    print("Turbo Universal Bot v4 Başlatılıyor...", flush=True)
    start = time.time()
    
    # 50 Bağlantı Limiti
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    async with AsyncSession() as session:
        cat_tasks = []
        for c, u in CATEGORIES.items():
            cat_tasks.append(scan_category(session, semaphore, c, u))
        await asyncio.gather(*cat_tasks)
    
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
