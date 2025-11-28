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
CONCURRENT_LIMIT = 5    # Hızlandırıldı
MAX_PAGES = 3           # Tarama derinliği
TIMEOUT = 20            # Zaman aşımı

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

FINAL_PLAYLIST = []

# --- YARDIMCI FONKSİYONLAR ---
def decrypt_aes(encrypted, password):
    try:
        key = password.encode('utf-8')
        if len(key) > 32: key = key[:32]
        elif len(key) < 32: key = key.ljust(32, b'\0')
        encrypted_bytes = base64.b64decode(encrypted)
        cipher = AES.new(key, AES.MODE_CBC, iv=key[:16])
        return unpad(cipher.decrypt(encrypted_bytes), AES.block_size).decode('utf-8', 'ignore')
    except:
        return None

async def fetch_text(session, url, referer=None):
    # Rastgele Browser İmzası (Engeli aşmak için)
    impersonations = ["chrome110", "chrome120", "edge101", "safari15_3"]
    headers = {"Referer": referer} if referer else {}
    
    try:
        response = await session.get(
            url, 
            headers=headers, 
            timeout=TIMEOUT, 
            impersonate=random.choice(impersonations)
        )
        if response.status_code == 200:
            return response.text
    except:
        pass
    return None

# --- VİDEO ÇÖZÜCÜ (AGRESİF) ---
async def resolve_any_video(session, iframe_url, referer_url):
    """
    Verilen iframe linki içindeki video dosyasını bulmaya çalışır.
    Oyuncu ayrımı yapmaz, ne bulursa alır.
    """
    if not iframe_url: return None
    if iframe_url.startswith("//"): iframe_url = "https:" + iframe_url

    # 1. Kaynak Kodunu Çek
    html = await fetch_text(session, iframe_url, referer=referer_url)
    if not html: return None

    # 2. CizgiDuo/Pass Özel Şifre Çözme
    if "cizgiduo" in iframe_url or "cizgipass" in iframe_url:
        match = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
        if match:
            pwd = match.group(1)
            json_str = match.group(2)
            data_match = re.search(r'"data"\s*:\s*"([^"]+)"', json_str)
            if data_match:
                decrypted = decrypt_aes(data_match.group(1), pwd)
                if decrypted:
                    m3u = re.search(r'video_location":"([^"]+)"', decrypted)
                    if m3u: return m3u.group(1).replace("\\", "")

    # 3. Sibnet Özel Çözme
    if "sibnet.ru" in iframe_url:
        slug_match = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html)
        if slug_match:
            slug = slug_match.group(1)
            return f"https://video.sibnet.ru{slug}" if slug.startswith("/") else slug

    # 4. GENEL TARAMA (En Önemlisi)
    # Sayfadaki herhangi bir .m3u8 veya .mp4 linkini yakala
    # Google Drive, Vidmoly, Fembed vb. ne varsa.
    
    # M3U8 Regex
    m3u8_matches = re.findall(r'(https?://[^"\'\s]+\.m3u8)', html)
    for m in m3u8_matches:
        if "google" not in m: # Google m3u8'leri genelde çalışmaz
            return m

    # MP4 Regex
    mp4_matches = re.findall(r'(https?://[^"\'\s]+\.mp4)', html)
    for m in mp4_matches:
        return m

    return None

async def process_episode(session, category, series_title, ep_title, ep_url, poster):
    # Bölüm sayfasına git
    html = await fetch_text(session, ep_url, referer=BASE_URL)
    if not html: return

    soup = BeautifulSoup(html, 'html.parser')
    
    # Sayfadaki TÜM Alternatif Kaynakları Bul
    # Genelde: ul.linkler li a
    links = soup.select("ul.linkler li a")
    
    found_url = None
    
    # Linkleri Karıştır (Hep aynı sırayla deneyip ban yememek için)
    # Ama Sibnet varsa ona öncelik ver çünkü o daha az patlıyor.
    link_data_list = []
    for l in links:
        d_frame = l.get("data-frame")
        if d_frame: link_data_list.append(d_frame)
    
    # Sibnet'i başa al
    link_data_list.sort(key=lambda x: "sibnet" not in x)

    # Sırayla tüm alternatifleri dene
    for iframe_src in link_data_list:
        video_url = await resolve_any_video(session, iframe_src, ep_url)
        if video_url:
            found_url = video_url
            break # Bulduk! Çık.
    
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
        print(f"  [+] Eklendi: {full_title} (Kaynak bulundu)", flush=True)
    else:
        # Eğer hiçbiri çalışmazsa, boşuna log basıp kirletme
        # Veya debug için basabilirsin
        print(f"  [-] Kaynak Yok: {series_title} - {ep_title}", flush=True)

async def process_series(session, semaphore, category, series_title, series_url, poster):
    async with semaphore:
        html = await fetch_text(session, series_url)
        if not html: return

        soup = BeautifulSoup(html, 'html.parser')
        # Bölümleri çek
        ep_divs = soup.select("div.asisotope div.ajax_post")
        
        tasks = []
        for div in ep_divs:
            name_s = div.select_one("span.episode-names")
            link_a = div.select_one("a")
            if name_s and link_a:
                tasks.append(process_episode(
                    session, category, series_title, 
                    name_s.text.strip(), link_a['href'], poster
                ))
        
        if tasks:
            print(f" > Dizi Taranıyor: {series_title} ({len(tasks)} Bölüm)", flush=True)
            # 20'li paketler halinde işle (Hızlandırıldı)
            chunk_size = 20
            for i in range(0, len(tasks), chunk_size):
                await asyncio.gather(*tasks[i:i+chunk_size])

async def scan_category(session, semaphore, cat_name, cat_url):
    print(f"\n--- Kategori: {cat_name} ---", flush=True)
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
        
        print(f" >> Sayfa {page}...", flush=True)
        html = await fetch_text(session, url)
        if not html: break
        
        soup = BeautifulSoup(html, 'html.parser')
        items = soup.select("ul.filter-results li")
        if not items: break
        
        tasks = []
        for item in items:
            t_tag = item.select_one("h2.truncate")
            l_tag = item.select_one("div.poster-subject a")
            i_tag = item.select_one("div.poster-media img")
            
            if t_tag and l_tag:
                poster = i_tag.get("data-src") if i_tag else ""
                tasks.append(process_series(
                    session, semaphore, cat_name, 
                    t_tag.text.strip(), l_tag['href'], poster
                ))
        
        await asyncio.gather(*tasks)

async def main():
    print("CizgiMax Final Bot v3...", flush=True)
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    async with AsyncSession() as session:
        for c_name, c_url in CATEGORIES.items():
            await scan_category(session, semaphore, c_name, c_url)
    
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik bulundu. M3U yazılıyor...", flush=True)
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt: pass
