import cloudscraper
from bs4 import BeautifulSoup
import re
import time
import random
import concurrent.futures
import sys
import json

# --- AYARLAR ---
MAX_WORKERS = 10      
MAX_SCROLLS = 50      
RETRY_COUNT = 3       

def log(message):
    print(message, flush=True)

# CloudScraper Ayarları
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'linux', # GitHub Actions Linux kullanıyor
        'desktop': True
    }
)

# --- HEADER AYARLARI (GÜNCELLENDİ) ---
# XMLHttpRequest başlığını buradan KALDIRDIK. Sadece POST işleminde kullanacağız.
scraper.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Referer": "https://www.google.com/",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
})

def get_current_domain():
    try:
        url = "https://raw.githubusercontent.com/Kraptor123/domainListesi/refs/heads/main/eklenti_domainleri.txt"
        response = scraper.get(url).text
        for line in response.splitlines():
            if line.strip().startswith("DiziPal"):
                domain = line.split(":")[-1].strip()
                if not domain.startswith("http"):
                    domain = "https://" + domain
                log(f"[+] Güncel Domain Bulundu: {domain}")
                return domain
    except:
        pass
    return "https://dizipal1217.com"

BASE_URL = get_current_domain()
API_URL = f"{BASE_URL}/api/load-movies" 

# Referer güncelle
scraper.headers.update({"Referer": f"{BASE_URL}/"})

def get_video_source(url):
    """Linkin içindeki video kaynağını bulur."""
    for _ in range(RETRY_COUNT):
        try:
            # Çok hızlı istek atmamak için bekleme
            time.sleep(random.uniform(0.1, 0.3))
            
            res = scraper.get(url)
            if res.status_code != 200: return None
            
            soup = BeautifulSoup(res.text, 'html.parser')

            # 1. Video Iframe Ara
            iframe = soup.select_one('.series-player-container iframe') or \
                     soup.select_one('div#vast_new iframe') or \
                     soup.select_one('iframe[src*="player"]')

            if iframe:
                src = iframe.get('src')
                if src:
                    if src.startswith("//"): src = "https:" + src
                    iframe_res = scraper.get(src, headers={"Referer": BASE_URL})
                    match = re.search(r'(?:file|source)\s*:\s*"([^"]+)"', iframe_res.text)
                    if match:
                        return match.group(1)

            # 2. Dizi Bölüm Listesi Ara
            episodes = soup.select('div.episode-item a') or \
                       soup.select('.episodes-list a') or \
                       soup.select('ul.episodes li a')
            
            if episodes:
                first_ep = episodes[0].get('href')
                if not first_ep.startswith("http"):
                    first_ep = BASE_URL + first_ep
                
                if first_ep != url:
                    return get_video_source(first_ep)

            return None
        except:
            continue
    return None

def process_single_content(item, category_name):
    try:
        link_tag = item.select_one('a')
        if not link_tag: return None

        title_tag = item.select_one('.title') or item.select_one('h5')
        title = title_tag.text.strip() if title_tag else link_tag.get('title', 'Bilinmeyen')

        img_tag = item.select_one('img')
        poster = img_tag.get('src') if img_tag else ""
        if poster and not poster.startswith("http"):
            poster = BASE_URL + poster 

        link = link_tag.get('href')
        if not link.startswith("http"):
            link = BASE_URL + link

        stream_url = get_video_source(link)
        
        if stream_url:
            m3u = f'#EXTINF:-1 group-title="{category_name}" tvg-logo="{poster}", {title}\n'
            m3u += f'#EXTVLCOPT:http-referrer={BASE_URL}/\n'
            m3u += f'#EXTHTTP:{{"Referer": "{BASE_URL}/"}}\n'
            m3u += f'{stream_url}\n'
            return m3u
    except:
        pass
    return None

def scrape_category(path, category_name):
    log(f"\n🚀 KATEGORİ BAŞLIYOR: {category_name}")
    entries = []
    
    first_url = f"{BASE_URL}{path}"
    
    try:
        res = scraper.get(first_url)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # --- DEBUG KISMI: Site ne döndürüyor? ---
        page_title = soup.title.string if soup.title else "Başlık Yok"
        log(f"   📄 Sayfa Başlığı: {page_title.strip()}")
        
        if "Just a moment" in page_title or "Cloudflare" in res.text:
            log("   ⛔ CLOUDFLARE KORUMASINA TAKILDI! (GitHub IP'si engellenmiş olabilir)")
            return []
        # ----------------------------------------

    except Exception as e:
        log(f"   🔥 Erişim Hatası: {e}")
        return []

    scroll_count = 0
    
    while scroll_count <= MAX_SCROLLS:
        # Genişletilmiş Seçiciler
        items = soup.select('article.movie-type-genres ul li') + \
                soup.select('div.episode-item') + \
                soup.select('article.type2 li') + \
                soup.select('.list-item') # Yedek
        
        items = [i for i in items if i.select_one('a')]
        
        if not items:
            log(f"   ⚠️ İçerik bulunamadı. HTML yapısı farklı olabilir veya liste bitti.")
            # Hata ayıklama için HTML'in bir kısmını göster (İsteğe bağlı açılabilir)
            # log(str(soup)[:500]) 
            break

        # İşlemleri Başlat
        found_on_load = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process_single_content, item, category_name) for item in items]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result:
                    entries.append(result)
                    found_on_load += 1
                    print(".", end="", flush=True)
        
        print("") 
        if found_on_load > 0:
            log(f"   ✅ Bu yüklemede {found_on_load} içerik eklendi.")

        # --- SONRAKİ SAYFA (AJAX POST) ---
        last_item = items[-1].select_one('a')
        if not last_item or not last_item.has_attr('data-id'):
            log("   🏁 'Daha Fazla Göster' verisi (data-id) bulunamadı. Kategori bitti.")
            break
            
        last_data_id = last_item.get('data-id')
        
        # Sadece burası için özel header
        post_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": BASE_URL,
            "Referer": first_url
        }
        
        payload = {
            'movie': last_data_id,
            'year': '',
            'tur': '',
            'siralama': ''
        }
        
        try:
            time.sleep(1.5) # API'yi yormamak için
            # cloudscraper ile POST, headerları merge eder
            api_res = scraper.post(API_URL, data=payload, headers=post_headers)
            
            try:
                json_data = api_res.json()
            except:
                log("   ❌ API yanıtı JSON değil. (Muhtemelen engellendi)")
                break
                
            if json_data.get('end') == True:
                log("   🏁 İçerik sonuna gelindi (API End).")
                break
                
            new_html = json_data.get('html')
            if not new_html:
                log("   ⚠️ API boş içerik döndü.")
                break
                
            soup = BeautifulSoup(new_html, 'html.parser')
            scroll_count += 1
            log(f"   🔄 {scroll_count}. sayfa yüklendi...")
            
        except Exception as e:
            log(f"   🔥 API Hatası: {e}")
            break

    return entries

def main():
    categories = [
        ("/diziler/son-bolumler", "Son Bölümler"),
        ("/filmler", "Filmler"),
        ("/diziler", "Diziler"),
        ("/koleksiyon/netflix", "Netflix"),
        ("/koleksiyon/exxen", "Exxen"),
        ("/koleksiyon/blutv", "BluTV"),
        ("/koleksiyon/disney", "Disney+"),
        ("/koleksiyon/amazon-prime", "Amazon Prime"),
        ("/koleksiyon/gain", "Gain"),
        ("/tur/mubi", "Mubi")
    ]
    
    with open("dizipal.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        
    total_count = 0
    
    for path, name in categories:
        cat_data = scrape_category(path, name)
        if cat_data:
            unique_data = list(set(cat_data))
            with open("dizipal.m3u", "a", encoding="utf-8") as f:
                f.writelines(unique_data)
            count = len(unique_data)
            total_count += count
            log(f"💾 {name} KAYDEDİLDİ. (+{count} içerik)")
        time.sleep(2)

    log(f"\n🎉 TÜM İŞLEM BİTTİ! Toplam {total_count} içerik 'dizipal.m3u' dosyasına yazıldı.")

if __name__ == "__main__":
    main()
