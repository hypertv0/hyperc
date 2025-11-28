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
CONCURRENT_LIMIT = 10   # Hızlandırıldı: Aynı anda 10 işlem
MAX_PAGES = 2           # Her kategoriden kaç sayfa taranacak?
TIMEOUT = 15            # 15 saniye cevap vermezse atla (Donmayı engeller)

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

# --- ŞİFRE ÇÖZME FONKSİYONLARI ---
def decrypt_cizgiduo(encrypted_data, password):
    """CizgiDuo AES Şifresini Çözer"""
    try:
        key = password.encode('utf-8')
        # Key 32 byte olmalı
        if len(key) > 32: key = key[:32]
        elif len(key) < 32: key = key.ljust(32, b'\0')

        encrypted_bytes = base64.b64decode(encrypted_data)
        # IV genelde Key ile aynıdır bu tür playerlarda
        cipher = AES.new(key, AES.MODE_CBC, iv=key[:16])
        try:
            decrypted = unpad(cipher.decrypt(encrypted_bytes), AES.block_size)
        except:
            # Padding hatası olursa raw decode dene
            decrypted = cipher.decrypt(encrypted_bytes)
            
        return decrypted.decode('utf-8', errors='ignore')
    except Exception as e:
        return None

# --- AĞ İSTEKLERİ ---
async def fetch_text(session, url, referer=None):
    """Url içeriğini çeker (Zaman aşımlı)"""
    headers = {"Referer": referer} if referer else {}
    try:
        # impersonate="chrome110" Cloudflare'i kandırır
        response = await session.get(url, headers=headers, timeout=TIMEOUT, impersonate="chrome110")
        if response.status_code == 200:
            return response.text
    except Exception:
        pass
    return None

async def resolve_video(session, iframe_url, referer_url):
    """Video linkini (M3U8) çözer"""
    if not iframe_url: return None
    if iframe_url.startswith("//"): iframe_url = "https:" + iframe_url

    # 1. CizgiDuo (AES Şifreli)
    if "cizgiduo" in iframe_url or "cizgipass" in iframe_url:
        try:
            html = await fetch_text(session, iframe_url, referer=referer_url)
            if not html: return None
            
            # bePlayer('password', '{data}')
            match = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
            if match:
                password = match.group(1)
                json_raw = match.group(2)
                
                # json içinden "data":"..." kısmını al
                data_match = re.search(r'"data"\s*:\s*"([^"]+)"', json_raw)
                if data_match:
                    enc_text = data_match.group(1)
                    decrypted = decrypt_cizgiduo(enc_text, password)
                    if decrypted:
                        # video_location":"url"
                        m3u = re.search(r'video_location":"([^"]+)"', decrypted)
                        if m3u:
                            return m3u.group(1).replace("\\", "")
        except:
            pass

    # 2. Sibnet
    elif "sibnet.ru" in iframe_url:
        try:
            # ID Çıkarma
            vid_id = None
            if "videoid=" in iframe_url:
                vid_id = re.search(r'videoid=(\d+)', iframe_url).group(1)
            elif "/video/" in iframe_url:
                vid_id = re.search(r'/video/(\d+)', iframe_url).group(1)
            
            if vid_id:
                real_url = f"https://video.sibnet.ru/video/{vid_id}"
                html = await fetch_text(session, real_url, referer=referer_url)
                if html:
                    slug = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html)
                    if slug:
                        path = slug.group(1)
                        return f"https://video.sibnet.ru{path}" if path.startswith("/") else path
        except:
            pass

    # 3. Genel M3U8/MP4 Tarama
    try:
        html = await fetch_text(session, iframe_url, referer=referer_url)
        if html:
            m3u8 = re.search(r'(https?://[^"\']+\.m3u8)', html)
            if m3u8: return m3u8.group(1)
            mp4 = re.search(r'(https?://[^"\']+\.mp4)', html)
            if mp4: return mp4.group(1)
    except:
        pass

    return None

async def process_episode(session, category, series_title, ep_title, ep_url, poster):
    html = await fetch_text(session, ep_url, referer=BASE_URL)
    if not html: return

    soup = BeautifulSoup(html, 'html.parser')
    links = soup.select("ul.linkler li a")
    
    # Kaynakları önceliklendir (CizgiDuo en iyi kalitedir)
    sorted_links = sorted(links, key=lambda x: (
        2 if "cizgiduo" in str(x.get("data-frame")) else
        1 if "sibnet" in str(x.get("data-frame")) else 0
    ), reverse=True)

    found_url = None
    for link in sorted_links:
        iframe = link.get("data-frame")
        video_url = await resolve_video(session, iframe, ep_url)
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
        print(f"  [+] Eklendi: {full_title}", flush=True)
    else:
        print(f"  [-] Bulunamadı: {series_title} - {ep_title}", flush=True)

async def process_series(session, semaphore, category, series_title, series_url, poster):
    async with semaphore:
        html = await fetch_text(session, series_url)
        if not html: return

        soup = BeautifulSoup(html, 'html.parser')
        ep_divs = soup.select("div.asisotope div.ajax_post")
        
        tasks = []
        for div in ep_divs:
            name_s = div.select_one("span.episode-names")
            link_a = div.select_one("a")
            if name_s and link_a:
                tasks.append(process_episode(session, category, series_title, name_s.text.strip(), link_a['href'], poster))
        
        if tasks:
            print(f" > Dizi: {series_title} ({len(tasks)} Bölüm)", flush=True)
            await asyncio.gather(*tasks)

async def scan_category(session, semaphore, cat_name, cat_url):
    print(f"\n--- Kategori: {cat_name} ---", flush=True)
    for page in range(1, MAX_PAGES + 1):
        # URL Oluşturma
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
        items = soup.select("ul.filter-results li")
        if not items: break
        
        tasks = []
        for item in items:
            t_tag = item.select_one("h2.truncate")
            l_tag = item.select_one("div.poster-subject a")
            i_tag = item.select_one("div.poster-media img")
            
            if t_tag and l_tag:
                poster = i_tag.get("data-src") if i_tag else ""
                tasks.append(process_series(session, semaphore, cat_name, t_tag.text.strip(), l_tag['href'], poster))
        
        await asyncio.gather(*tasks)

async def main():
    print("Bot Başlatılıyor...", flush=True)
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    async with AsyncSession() as session:
        tasks = []
        for c_name, c_url in CATEGORIES.items():
            tasks.append(scan_category(session, semaphore, c_name, c_url))
        await asyncio.gather(*tasks)
    
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik bulundu. Kaydediliyor...", flush=True)
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
    except KeyboardInterrupt:
        print("Durduruldu.")
