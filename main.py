import asyncio
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
import re
import time
import base64
import sys
import random
import xml.etree.ElementTree as ET
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# --- AYARLAR ---
BASE_URL = "https://cizgimax.online"
SITEMAP_URL = "https://cizgimax.online/sitemap.xml"
OUTPUT_FILE = "cizgimax.m3u"
CONCURRENT_LIMIT = 50   # 50 Eşzamanlı bağlantı (Sitemap olduğu için hızlı olabiliriz)
TIMEOUT = 15            # İstek zaman aşımı

# Sonuç Listesi
FINAL_PLAYLIST = []
# İşlenecek Linkler Kuyruğu
URL_QUEUE = set() # Aynı linki 2 kere eklememek için set kullanıyoruz

# --- ŞİFRE ÇÖZME (KOTLIN PORTU) ---
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

# --- AĞ İSTEKLERİ ---
async def fetch_text(session, url, referer=None):
    headers = {"Referer": referer if referer else BASE_URL}
    try:
        # Browser taklidi
        response = await session.get(url, headers=headers, timeout=TIMEOUT, impersonate="chrome120")
        if response.status_code == 200:
            return response.text
    except:
        pass
    return None

# --- SITEMAP AYRIŞTIRICI ---
async def parse_sitemap(session):
    print("Sitemap Haritası İndiriliyor...", flush=True)
    
    # 1. Ana Sitemap'i çek
    xml_text = await fetch_text(session, SITEMAP_URL)
    if not xml_text:
        print("!!! Sitemap çekilemedi. Site engelliyor olabilir.", flush=True)
        return

    # XML Parse
    try:
        # XML namespace temizliği (bazen ns0:loc gibi gelir)
        xml_text = re.sub(r' xmlns="[^"]+"', '', xml_text, count=1)
        root = ET.fromstring(xml_text)
        
        sub_sitemaps = []
        # Sitemap index mi yoksa urlset mi?
        if root.tag.endswith("sitemapindex"):
            for sitemap in root.findall("sitemap"):
                loc = sitemap.find("loc").text
                # Genellikle 'post-sitemap' dizileri/bölümleri içerir
                if "post-sitemap" in loc or "diziler-sitemap" in loc:
                    sub_sitemaps.append(loc)
        else:
            # Tek sitemap ise direkt işle
            sub_sitemaps.append(SITEMAP_URL)

        print(f"Bulunan Alt Sitemap Sayısı: {len(sub_sitemaps)}", flush=True)

        # 2. Alt Sitemapleri çek ve URL'leri topla
        for sub_url in sub_sitemaps:
            print(f" > Taranıyor: {sub_url}", flush=True)
            sub_xml = await fetch_text(session, sub_url)
            if not sub_xml: continue
            
            sub_xml = re.sub(r' xmlns="[^"]+"', '', sub_xml, count=1)
            sub_root = ET.fromstring(sub_xml)
            
            for url_tag in sub_root.findall("url"):
                loc = url_tag.find("loc").text
                # Sadece bölüm izleme sayfalarını al (diziler/dizi-adi-bolum-izle)
                # Cloudstream mantığına göre filtreleme:
                if "/diziler/" in loc and ("-izle" in loc or "bolum" in loc):
                    URL_QUEUE.add(loc)
                    
        print(f"\nToplam {len(URL_QUEUE)} adet bölüm linki bulundu!", flush=True)
        
    except Exception as e:
        print(f"Sitemap Hatası: {e}", flush=True)

# --- VİDEO ÇÖZÜCÜ ---
async def resolve_video(session, iframe_url, referer_url):
    if not iframe_url: return None
    if iframe_url.startswith("//"): iframe_url = "https:" + iframe_url

    # CizgiDuo (AES)
    if "cizgiduo" in iframe_url or "cizgipass" in iframe_url:
        html = await fetch_text(session, iframe_url, referer=referer_url)
        if html:
            m = re.search(r"bePlayer\('([^']+)',\s*'(\{[^}]+\})'\)", html)
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
            html = await fetch_text(session, real, referer=referer_url)
            if html:
                slug = re.search(r'player\.src\(\[\{src:\s*"([^"]+)"', html)
                if slug:
                    s = slug.group(1)
                    return f"https://video.sibnet.ru{s}" if s.startswith("/") else s

    # Genel (M3U8)
    else:
        html = await fetch_text(session, iframe_url, referer=referer_url)
        if html:
            m3u = re.search(r'(https?://[^"\'\s]+\.m3u8)', html)
            if m3u: return m3u.group(1)
            mp4 = re.search(r'(https?://[^"\'\s]+\.mp4)', html)
            if mp4: return mp4.group(1)

    return None

# --- BÖLÜM İŞLEME ---
async def process_page(session, semaphore, url):
    async with semaphore:
        html = await fetch_text(session, url)
        if not html: return

        soup = BeautifulSoup(html, 'html.parser')
        
        # 1. Başlık ve Kategori Bilgilerini Al
        # Başlık genellikle h1 veya title'da olur
        # Cloudstream: h1.page-title
        h1 = soup.select_one("h1.page-title")
        if not h1: 
            # Yedek başlık alımı
            h1 = soup.select_one("title")
            title_text = h1.text.replace("izle", "").replace("CizgiMax", "").strip() if h1 else "Bilinmeyen Dizi"
        else:
            title_text = h1.text.strip()

        # Dizi Adı ve Bölüm Adını Ayır
        # Format genelde: "Dizi Adı 1. Sezon 1. Bölüm izle"
        # Biz bunu "Dizi Adı - 1. Sezon 1. Bölüm" yapacağız
        
        series_title = title_text
        episode_title = title_text
        
        # Kategori Al (Breadcrumb veya Etiketlerden)
        # Cloudstream: div.genre-item a
        genres = soup.select("div.genre-item a")
        category = "Genel"
        if genres:
            # İlk geçerli kategoriyi al (Yıl olmayan)
            for g in genres:
                g_text = g.text.strip()
                if not g_text.isdigit(): 
                    category = g_text
                    break

        # 2. Poster Al
        img = soup.select_one("img.series-profile-thumb")
        poster = img.get("src") if img else ""

        # 3. Video Linklerini Bul
        links = soup.select("ul.linkler li a")
        
        # Linkleri Sırala (Sibnet > Cizgi > Diğer)
        sorted_links = sorted(links, key=lambda x: (
            2 if "sibnet" in str(x.get("data-frame")) else
            1 if "cizgi" in str(x.get("data-frame")) else 0
        ), reverse=True)

        found_url = None
        for link in sorted_links:
            src = link.get("data-frame")
            found_url = await resolve_video(session, src, url)
            if found_url: break
        
        if found_url:
            # M3U Formatına Uygun Başlık Düzenleme
            # "Gumball 1. Sezon 5. Bölüm" -> Group: Gumball, Title: 1. Sezon 5. Bölüm
            # Ancak HTML player için "Dizi Adı - Bölüm" formatı en iyisidir.
            
            # Basit bir temizlik
            clean_title = title_text.replace("izle", "").strip()
            
            FINAL_PLAYLIST.append({
                "group": category,
                "title": clean_title,
                "logo": poster,
                "url": found_url
            })
            print(f"  [+] {clean_title}", flush=True)
        # else: print(f"  [-] Kaynak Yok: {title_text}", flush=True)

async def main():
    print("Sitemap Tabanlı CizgiMax Botu Başlatılıyor...", flush=True)
    start_time = time.time()
    
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    async with AsyncSession(impersonate="chrome120") as session:
        # 1. Sitemap'ten tüm linkleri bul
        await parse_sitemap(session)
        
        if not URL_QUEUE:
            print("Hiç link bulunamadı! Program sonlandırılıyor.", flush=True)
            return

        print(f"İşlem Başlıyor: {len(URL_QUEUE)} adet sayfa taranacak...", flush=True)
        
        # 2. Linkleri İşle (Task Havuzu)
        tasks = []
        for url in URL_QUEUE:
            tasks.append(process_page(session, semaphore, url))
        
        # Parçalı işlem (Her seferinde 500 görev, RAM şişmesini önlemek için)
        chunk_size = 500
        total_tasks = len(tasks)
        
        for i in range(0, total_tasks, chunk_size):
            chunk = tasks[i:i + chunk_size]
            print(f" >> Paket İşleniyor: {i} - {i + len(chunk)} / {total_tasks}", flush=True)
            await asyncio.gather(*chunk)
            
    print(f"\nToplam {len(FINAL_PLAYLIST)} içerik bulundu. Kaydediliyor...", flush=True)
    
    # Sıralama: Kategori -> İsim
    FINAL_PLAYLIST.sort(key=lambda x: (x["group"], x["title"]))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in FINAL_PLAYLIST:
            f.write(f'#EXTINF:-1 group-title="{item["group"]}" tvg-logo="{item["logo"]}", {item["title"]}\n')
            f.write(f'{item["url"]}\n')

    print(f"Tamamlandı! Süre: {time.time() - start_time:.2f}sn")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt: pass
