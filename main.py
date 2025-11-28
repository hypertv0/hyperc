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
CONCURRENT_LIMIT = 20   # Hız/Güvenlik Dengesi (20 ideal)
MAX_PAGES = 3           # Her kategoriden taranacak sayfa sayısı
TIMEOUT = 15            # İstek zaman aşımı (saniye)

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

# --- AĞ İSTEKLERİ ---
async def fetch_text(session, url, referer=None):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer if referer else BASE_URL
    }
    try:
        # impersonate="chrome110" Cloudflare'i geçmek için en hızlısı
        response = await session.get(url, headers=headers, timeout=TIMEOUT, impersonate="chrome110")
        if response.status_code == 200:
            return response.text
        elif response.status_code == 403:
            return "403"
    except:
        pass
    return None

# --- KAYNAK ÇÖZÜCÜLER ---
async def resolve_video_url(session, iframe_src, referer_url):
    """Verilen iframe linkini çözer"""
    if not iframe_src: return None
    if iframe_src.startswith("//"): iframe_src = "https:" + iframe_src

    # 1. CizgiDuo/Pass
    if "cizgiduo" in iframe_src or "cizgipass" in iframe_src:
        html = await fetch_text(session, iframe_src, referer=referer_url)
        if html and html != "403":
            match = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
            if match:
                pwd = match.group(1)
                json_raw = match.group(2)
                data_match = re.search(r'"data"\s*:\s*"([^"]+)"', json_raw)
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
        if html and html != "403":
            m3u = re.search(r'(https?://[^"\'\s]+\.m3u8)', html)
            if m3u: return m3u.group(1)
            mp4 = re.search(r'(https?://[^"\'\s]+\.mp4)', html)
            if mp4: return mp4.group(1)
            
    return None

# --- İŞLEMCİLER ---
async def process_episode(session, semaphore, category, series_title, ep_title, ep_url, poster):
    """Tek bir bölümü işler"""
    async with semaphore:
        html = await fetch_text(session, ep_url, referer=BASE_URL)
        if not html: return

        soup = BeautifulSoup(html, 'html.parser')
        links = soup.select("ul.linkler li a")
        
        # Linkleri sırala (Cizgi -> Sibnet -> Diğer)
        sorted_links = sorted(links, key=lambda x: (
            2 if "cizgi" in str(x.get("data-frame")) else
            1 if "sibnet" in str(x.get("data-frame")) else 0
        ), reverse=True)

        found_url = None
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
            print(f"  [+] {full_title}", flush=True)
        else:
            pass

async def process_series(session, semaphore, category, series_title, series_url, poster):
    """Dizi sayfasını tarar"""
    async with semaphore:
        html = await fetch_text(session, series_url)
    
    if not html: return

    soup = BeautifulSoup(html, 'html.parser')
    ep_divs = soup.select("div.asisotope div.ajax_post")
    
    if ep_divs:
        print(f" > {series_title} ({len(ep_divs)} Bölüm) işleniyor...", flush=True)
    
    tasks = []
    for div in ep_divs:
        name_s = div.select_one("span.episode-names")
        link_a = div.select_one("a")
        if name_s and link_a:
            tasks.append(process_episode(
                session, semaphore, category, series_title, 
                name_s.text.strip(), link_a['href'], poster
            ))
            
    await asyncio.gather(*tasks)

async def scan_category(session, semaphore, cat_name, cat_url):
    """Kategoriyi tarar"""
    print(f"--- {cat_name} Başlatıldı ---", flush=True)
    
    series_tasks = []
    
    for page in range(1, MAX_PAGES + 1):
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
                series_tasks.append(process_series(
                    session, semaphore, cat_name, 
                    t_tag.text.strip(), l_tag['href'], poster
                ))
    
    await asyncio.gather(*series_tasks)

async def main():
    print("Turbo CizgiMax Bot Başlatılıyor...", flush=True)
    start_time = time.time()
    
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    async with AsyncSession() as session:
        cat_tasks = []
        for c_name, c_url in CATEGORIES.items():
            cat_tasks.append(scan_category(session, semaphore, c_name, c_url))
        
        await asyncio.gather(*cat_tasks)
    
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik bulundu. M3U yazılıyor...", flush=True)
    
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            # HATANIN DÜZELTİLDİĞİ SATIR:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

    print(f"Bitti! Süre: {time.time() - start_time:.2f}sn")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt: pass
