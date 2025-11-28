import asyncio
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
import re
import time
import base64
import json
import sys
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# --- AYARLAR ---
BASE_URL = "https://cizgimax.online"
OUTPUT_FILE = "cizgimax.m3u"
CONCURRENT_LIMIT = 5    # Hız ve Güvenlik Dengesi
MAX_PAGES = 3           # Tarama derinliği
TIMEOUT = 25            # Zaman aşımı

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

# --- KOTLIN AESHelper PORTU ---
def decrypt_aes_cizgiduo(encrypted_data, password):
    """
    Cloudstream AesHelper.cryptoAESHandler mantığının Python karşılığı.
    Key: Password (32 byte'a tamamlanmış)
    IV: Key'in ilk 16 byte'ı (Genellikle) veya Password (16 byte'a tamamlanmış)
    Mode: CBC
    """
    try:
        # Kotlin'deki AesHelper genelde key'i byte array'e çevirir.
        # Eğer key 32 byte'dan kısaysa null byte ile değil, tekrar ederek veya özel padding ile doldurur.
        # Ancak web playerlarda genelde Key direkt kullanılır.
        
        key_bytes = password.encode('utf-8')
        
        # Key uzunluğunu 32 byte'a (AES-256) tamamla/kes
        if len(key_bytes) > 32:
            key_bytes = key_bytes[:32]
        elif len(key_bytes) < 32:
            # Basit zero padding (Web player standardı)
            key_bytes = key_bytes.ljust(32, b'\0')

        # IV: Genellikle Key'in ilk 16 byte'ıdır
        iv_bytes = key_bytes[:16]

        # Base64 Decode
        encrypted_bytes = base64.b64decode(encrypted_data)

        # Decrypt
        cipher = AES.new(key_bytes, AES.MODE_CBC, iv=iv_bytes)
        decrypted_bytes = cipher.decrypt(encrypted_bytes)
        
        # Unpad (PKCS7)
        try:
            decrypted_text = unpad(decrypted_bytes, AES.block_size).decode('utf-8')
        except:
            # Padding hatası olursa raw decode dene (bazen padding olmaz)
            decrypted_text = decrypted_bytes.decode('utf-8', errors='ignore')
            # Temizlik
            decrypted_text = decrypted_text.strip().replace('\x00', '') # Null byte temizle

        return decrypted_text
    except Exception as e:
        # print(f"Decryption Error: {e}") 
        return None

# --- AĞ İSTEKLERİ ---
async def fetch_text(session, url, referer=None):
    """Cloudflare'i taklit ederek istek atar"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer if referer else BASE_URL
    }
    
    try:
        # 'chrome110' impersonate Cloudflare için kritiktir.
        response = await session.get(url, headers=headers, timeout=TIMEOUT, impersonate="chrome110")
        if response.status_code == 200:
            return response.text
        elif response.status_code == 403:
            return "403_FORBIDDEN"
    except Exception:
        pass
    return None

# --- EXTRACTORLAR (KOTLIN PORTLARI) ---

async def extract_cizgiduo(session, iframe_url, referer_url):
    """CizgiDuo ve CizgiPass Extractor"""
    html = await fetch_text(session, iframe_url, referer=referer_url)
    
    if html == "403_FORBIDDEN":
        return None # IP Ban, yapacak bir şey yok
    if not html: return None

    # Regex: bePlayer('pass', '{data}')
    # Kotlin: Regex("""bePlayer\('([^']+)',\s*'(\{[^}]+\})'\);""")
    match = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
    
    if match:
        password = match.group(1)
        json_raw = match.group(2)
        
        # JSON içinden şifreli datayı al
        data_match = re.search(r'"data"\s*:\s*"([^"]+)"', json_raw)
        if data_match:
            encrypted_data = data_match.group(1)
            
            # AES Çözme
            decrypted = decrypt_aes_cizgiduo(encrypted_data, password)
            if decrypted:
                # video_location":"https://..."
                m3u_match = re.search(r'video_location":"([^"]+)"', decrypted)
                if m3u_match:
                    return m3u_match.group(1).replace("\\", "")
    return None

async def extract_sibnet(session, iframe_url, referer_url):
    """SibNet Extractor"""
    # Kotlin kodunda linkler | ile ayrılabiliyor, temizle
    if "|" in iframe_url:
        iframe_url = iframe_url.split("|")[0]

    # Video ID'yi bul ve gerçek sayfaya git (shell.php bypass)
    video_id = None
    if "videoid=" in iframe_url:
        video_id = re.search(r'videoid=(\d+)', iframe_url).group(1)
    elif "/video/" in iframe_url:
        video_id = re.search(r'/video/(\d+)', iframe_url).group(1)
    
    if video_id:
        real_url = f"https://video.sibnet.ru/video/{video_id}"
        html = await fetch_text(session, real_url, referer=referer_url)
        
        if html:
            # Kotlin: player.src([{src: "..."
            slug_match = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html)
            if slug_match:
                slug = slug_match.group(1)
                if slug.startswith("/"):
                    return f"https://video.sibnet.ru{slug}"
                return slug
    return None

async def extract_generic(session, iframe_url, referer_url):
    """Genel M3U8 Tarayıcı (Yedek Plan)"""
    html = await fetch_text(session, iframe_url, referer=referer_url)
    if html and html != "403_FORBIDDEN":
        # .m3u8 linkini bul
        m3u8 = re.search(r'(https?://[^"\'\s]+\.m3u8)', html)
        if m3u8: return m3u8.group(1)
        
        # .mp4 linkini bul
        mp4 = re.search(r'(https?://[^"\'\s]+\.mp4)', html)
        if mp4: return mp4.group(1)
    return None

async def process_episode(session, category, series_title, ep_title, ep_url, poster):
    # Bölüm sayfasına git
    html = await fetch_text(session, ep_url, referer=BASE_URL)
    if not html: return

    soup = BeautifulSoup(html, 'html.parser')
    
    # Kotlin: document.select("ul.linkler li")
    links = soup.select("ul.linkler li a")
    
    found_url = None
    
    # Öncelik Sırası: CizgiDuo/Pass > Sibnet > Diğerleri
    sorted_links = sorted(links, key=lambda x: (
        2 if "cizgi" in str(x.get("data-frame")) else
        1 if "sibnet" in str(x.get("data-frame")) else 0
    ), reverse=True)

    for link in sorted_links:
        iframe_src = link.get("data-frame")
        if not iframe_src: continue
        
        # Protokol düzeltme
        if iframe_src.startswith("//"): iframe_src = "https:" + iframe_src
        
        video_url = None
        
        # --- EXTRACTOR SEÇİMİ ---
        if "cizgiduo" in iframe_src or "cizgipass" in iframe_src:
            video_url = await extract_cizgiduo(session, iframe_src, ep_url)
            
        elif "sibnet.ru" in iframe_src:
            video_url = await extract_sibnet(session, iframe_src, ep_url)
            
        else:
            video_url = await extract_generic(session, iframe_src, ep_url)
        
        if video_url:
            found_url = video_url
            break # Link bulundu, döngüden çık
    
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
        print(f"  [+] Eklendi: {full_title}", flush=True)
    else:
        # Detaylı Log
        # print(f"  [-] Kaynak Yok: {series_title} - {ep_title}", flush=True)
        pass

async def process_series(session, semaphore, category, series_title, series_url, poster):
    async with semaphore:
        html = await fetch_text(session, series_url)
        if not html: return

        soup = BeautifulSoup(html, 'html.parser')
        # Kotlin: document.select("div.asisotope div.ajax_post")
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
            print(f" > Dizi: {series_title} ({len(tasks)} Bölüm)", flush=True)
            # Hepsini aynı anda başlatma, küçük gruplarla yap
            chunk_size = 10
            for i in range(0, len(tasks), chunk_size):
                await asyncio.gather(*tasks[i:i+chunk_size])

async def scan_category(session, semaphore, cat_name, cat_url):
    print(f"\n--- Kategori: {cat_name} ---", flush=True)
    for page in range(1, MAX_PAGES + 1):
        # URL Yapılandırması
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
        
        print(f" >> Sayfa {page} taranıyor...", flush=True)
        html = await fetch_text(session, url)
        if not html: break
        
        soup = BeautifulSoup(html, 'html.parser')
        # Kotlin: document.select("ul.filter-results li")
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
    print("CizgiMax Kotlin-to-Python Bot Başlatılıyor...", flush=True)
    
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    async with AsyncSession() as session:
        for c_name, c_url in CATEGORIES.items():
            await scan_category(session, semaphore, c_name, c_url)
    
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik bulundu. M3U yazılıyor...", flush=True)
    
    # Sıralama: Kategori -> Dizi Adı -> Bölüm Adı
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
