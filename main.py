import asyncio
import re
import time
import base64
import sys
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from seleniumbase import SB

# --- AYARLAR ---
BASE_URL = "https://cizgimax.online"
OUTPUT_FILE = "cizgimax.m3u"
CONCURRENT_LIMIT = 30   # Cookie aldığımız için hız sınırını artırabiliriz
TIMEOUT = 20            # İstek zaman aşımı

# Hedef Sitemapler (Manuel Liste - Hız ve Kesinlik İçin)
# post-sitemap.xml (En yeni) -> post-sitemap33.xml (En eski)
TARGET_SITEMAPS = [f"https://cizgimax.online/post-sitemap{i}.xml" if i > 1 else "https://cizgimax.online/post-sitemap.xml" for i in range(1, 15)]
# Not: 1'den 15'e kadar olan sitemapleri (yaklaşık 15.000 bölüm) tarar. Hepsini istersen 34 yap.

FINAL_PLAYLIST = []
URL_QUEUE = asyncio.Queue()
PROCESSED = 0
TOTAL_URLS = 0

# --- ŞİFRE ÇÖZME ---
def decrypt_aes(encrypted, password):
    try:
        key = password.encode('utf-8')
        key = key[:32].ljust(32, b'\0')
        iv = key[:16]
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        try: dec = unpad(cipher.decrypt(base64.b64decode(encrypted)), AES.block_size)
        except: dec = cipher.decrypt(base64.b64decode(encrypted))
        return dec.decode('utf-8', 'ignore').strip().replace('\x00', '')
    except: return None

# --- VİDEO ÇÖZÜCÜ ---
async def resolve_video(session, iframe_url, referer_url):
    if not iframe_url: return None
    if iframe_url.startswith("//"): iframe_url = "https:" + iframe_url

    try:
        # CizgiDuo / Pass
        if "cizgi" in iframe_url:
            resp = await session.get(iframe_url, headers={"Referer": referer_url}, timeout=TIMEOUT)
            if resp.status_code == 200:
                m = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", resp.text)
                if m:
                    data_match = re.search(r'"data"\s*:\s*"([^"]+)"', m.group(2))
                    if data_match:
                        dec = decrypt_aes(data_match.group(1), m.group(1))
                        if dec:
                            res = re.search(r'video_location":"([^"]+)"', dec)
                            if res: return res.group(1).replace("\\", "")

        # Sibnet
        elif "sibnet.ru" in iframe_url:
            if "|" in iframe_url: iframe_url = iframe_url.split("|")[0]
            vid = re.search(r'(?:videoid=|/video/)(\d+)', iframe_url)
            if vid:
                real = f"https://video.sibnet.ru/video/{vid.group(1)}"
                resp = await session.get(real, headers={"Referer": referer_url}, timeout=TIMEOUT)
                if resp.status_code == 200:
                    slug = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', resp.text)
                    if slug:
                        s = slug.group(1)
                        return f"https://video.sibnet.ru{s}" if s.startswith("/") else s

        # Genel
        else:
            resp = await session.get(iframe_url, headers={"Referer": referer_url}, timeout=TIMEOUT)
            if resp.status_code == 200:
                m3u = re.search(r'(https?://[^"\'\s]+\.m3u8)', resp.text)
                if m3u: return m3u.group(1)
                mp4 = re.search(r'(https?://[^"\'\s]+\.mp4)', resp.text)
                if mp4: return mp4.group(1)

    except: pass
    return None

# --- İŞÇİ (WORKER) ---
async def worker(session):
    global PROCESSED
    while True:
        try:
            page_url = await URL_QUEUE.get()
        except asyncio.QueueEmpty:
            break

        try:
            response = await session.get(page_url, timeout=TIMEOUT)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Başlık
                h1 = soup.select_one("h1.page-title")
                if not h1: h1 = soup.select_one("title")
                
                title_text = h1.text.strip() if h1 else "Bilinmeyen"
                # Temizlik
                clean_title = title_text.replace("izle", "").replace("ÇizgiMax", "").strip()
                # Gereksiz tireleri sil
                clean_title = re.sub(r'^[-:\s]+|[-:\s]+$', '', clean_title)

                # Kategori (Dizi Adı)
                # Breadcrumb'dan al: Anasayfa > Dizi Adı > Sezon...
                crumbs = soup.select("div.Breadcrumb span[itemprop='name']")
                category = "Genel"
                if len(crumbs) > 1:
                    category = crumbs[1].text.strip()
                
                # Poster
                img = soup.select_one("img.series-profile-thumb")
                poster = img.get("src") if img else ""

                # Video Linkleri
                links = soup.select("ul.linkler li a")
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
                        "title": clean_title,
                        "logo": poster,
                        "url": found_url
                    })
                    # print(f"  [+] {clean_title}") # Çok hızlı akacağı için kapattım

        except Exception:
            pass
        
        PROCESSED += 1
        if PROCESSED % 100 == 0:
            print(f"İlerleme: {PROCESSED}/{TOTAL_URLS} tamamlandı.", flush=True)
        
        URL_QUEUE.task_done()

# --- ANA PROGRAM ---
async def async_crawler(cookies, user_agent):
    global TOTAL_URLS
    print("Hızlı Tarama Başlatılıyor...", flush=True)
    
    # Selenium'dan aldığımız Cookie'leri kullanıyoruz
    async with AsyncSession(cookies=cookies, headers={"User-Agent": user_agent}, impersonate="chrome120") as session:
        
        # 1. Sitemaps İndir ve Parse Et
        print("Sitemapler İndiriliyor...", flush=True)
        for sitemap in TARGET_SITEMAPS:
            try:
                resp = await session.get(sitemap, timeout=30)
                if resp.status_code == 200:
                    # XML mi HTML mi kontrol et (WordPress eklentisi HTML tablo döndürebilir)
                    content = resp.text
                    
                    # Eğer HTML tablosu ise (Önceki sorunun çözümü)
                    if "<table" in content:
                        # HTML içindeki linkleri topla
                        urls = re.findall(r'href="(https://cizgimax\.online/[^"]+)"', content)
                    else:
                        # Saf XML ise
                        urls = re.findall(r'<loc>(https://cizgimax\.online/[^<]+)</loc>', content)
                    
                    count = 0
                    for url in urls:
                        # Filtrele
                        if "-izle" in url or "bolum" in url:
                            URL_QUEUE.put_nowait(url)
                            count += 1
                    print(f" > {sitemap}: {count} link bulundu.")
                    TOTAL_URLS += count
            except Exception as e:
                print(f" > {sitemap} Hatası: {e}")

        if TOTAL_URLS == 0:
            print("Hiç link bulunamadı! Cloudflare hala engelliyor olabilir.")
            return

        print(f"\nToplam {TOTAL_URLS} içerik işlenecek. {CONCURRENT_LIMIT} işçi çalışıyor...", flush=True)
        
        # 2. İşçileri Başlat
        workers = [asyncio.create_task(worker(session)) for _ in range(CONCURRENT_LIMIT)]
        await URL_QUEUE.join()
        for w in workers: w.cancel()

def main():
    print("1. Aşama: Selenium ile Cloudflare Kırılıyor...")
    cf_cookies = {}
    user_agent = ""
    
    with SB(uc=True, headless=True) as sb:
        try:
            sb.open(BASE_URL)
            # Cloudflare kontrolü için biraz bekle
            time.sleep(6) 
            if "Just a moment" in sb.get_title():
                print("Cloudflare ekranı tespit edildi, bekleniyor...")
                time.sleep(10)
            
            # Cookie ve UA al
            cookies = sb.get_cookies()
            for cookie in cookies:
                cf_cookies[cookie['name']] = cookie['value']
            
            user_agent = sb.get_user_agent()
            print("Giriş Başarılı! Kimlik bilgileri kopyalandı.")
            
        except Exception as e:
            print(f"Selenium Hatası: {e}")
            return

    # Selenium işini bitirdi, kapatıldı. Şimdi Turbo mod (Asyncio) başlıyor.
    print("2. Aşama: Hızlı Tarama (Asyncio + Curl_cffi)...")
    
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(async_crawler(cf_cookies, user_agent))
    
    # Kaydet
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik bulundu. M3U oluşturuluyor...")
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

    print("Bitti.")

if __name__ == "__main__":
    main()
